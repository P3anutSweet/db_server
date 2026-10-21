#!/usr/bin/env python3
"""Transactional PostgreSQL row journal and durable WD export. Root only.

install previews by default; install --apply installs capture and a15-second
export timer. --cleanup invokes the separately reviewed API cleanup as jjj only
after baseline export and rollback-aware trigger tests have passed.
"""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

ROOT=Path('/etc/teable')
CONFIG=Path('/etc/db-server/record-changes.json')
STATUS=Path('/var/lib/db-server/record-change-status.json')
DEST=Path('/srv/backups/teable-record-changes')
EXPORT_LOCK=Path('/run/lock/db-record-changes.lock')
COMPOSE=['docker','compose','--project-directory',str(ROOT),'--env-file',str(ROOT/'.env'),'-f',str(ROOT/'compose.yaml')]
BASE='bseCHXUwSZVcKSUFqDQ'


def literal(value):
    return "'"+str(value).replace("'","''")+"'"


def identifier(value):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',value):
        raise ValueError('Unexpected physical SQL identifier.')
    return '"'+value+'"'


def qualified(value):
    parts=value.split('.')
    if len(parts)!=2:
        raise ValueError('Expected schema-qualified table name.')
    return '.'.join(identifier(p) for p in parts)


def sql(statement):
    result=subprocess.run(COMPOSE+['exec','-T','db','psql','-X','-qAt','-U','teable','-d','teable',
        '-v','ON_ERROR_STOP=1'],input=statement,text=True,capture_output=True)
    if result.returncode:
        # SQL/record values and arbitrary database error echoes are private.
        raise RuntimeError('PostgreSQL operation failed; no SQL or record data echoed.')
    return result.stdout.strip()


def mount_check():
    cfg=json.loads(Path('/etc/db-server/storage.json').read_text())
    for mount,key in [('/srv/db','db_uuid'),('/srv/backups','backup_uuid')]:
        actual=subprocess.check_output(['findmnt','-rn','--mountpoint',mount,'-o','UUID'],text=True).strip()
        if actual!=cfg[key]:
            raise RuntimeError('Expected database/WD filesystem is not mounted.')


DDL='''
CREATE SCHEMA IF NOT EXISTS db_server_audit;
REVOKE ALL ON SCHEMA db_server_audit FROM PUBLIC;
CREATE TABLE IF NOT EXISTS db_server_audit.settings(key text PRIMARY KEY,value text NOT NULL);
CREATE TABLE IF NOT EXISTS db_server_audit.targets(
 physical text PRIMARY KEY,table_id text NOT NULL,record_key text NOT NULL,
 kind text NOT NULL,field_id text NOT NULL,excluded text[] NOT NULL);
CREATE TABLE IF NOT EXISTS db_server_audit.events(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),txid bigint NOT NULL DEFAULT txid_current(),
 operation text NOT NULL,kind text NOT NULL,table_id text NOT NULL,field_id text,
 record_id text,physical_table text NOT NULL,actor_hint text,db_actor text NOT NULL DEFAULT session_user,
 before_value jsonb,after_value jsonb);
CREATE INDEX IF NOT EXISTS audit_record_idx ON db_server_audit.events(table_id,record_id,id);
CREATE TABLE IF NOT EXISTS db_server_audit.deliveries(
 event_id bigint PRIMARY KEY REFERENCES db_server_audit.events(id),
 path text NOT NULL,sha256 text NOT NULL,exported_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE OR REPLACE FUNCTION db_server_audit.project_row(image jsonb,target text,excluded text[])
RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog AS $$
 SELECT COALESCE(jsonb_object_agg(e.key,e.value),'{}'::jsonb) FROM jsonb_each(image) e
 WHERE left(e.key,2)='__' OR e.key IN
 (SELECT db_field_name FROM public.field WHERE table_id=target AND deleted_time IS NULL
 AND NOT(db_field_name=ANY(excluded)))
$$;
CREATE OR REPLACE FUNCTION db_server_audit.capture() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,db_server_audit AS $$
DECLARE cfg db_server_audit.targets%ROWTYPE; b jsonb; a jsonb; row_data jsonb; target_id text;
BEGIN
 SELECT * INTO STRICT cfg FROM db_server_audit.targets WHERE physical=TG_ARGV[0];
 IF TG_OP<>'INSERT' THEN b=to_jsonb(OLD)-cfg.excluded; END IF;
 IF TG_OP<>'DELETE' THEN a=to_jsonb(NEW)-cfg.excluded; END IF;
 IF cfg.kind='record' THEN
   IF b IS NOT NULL THEN b=db_server_audit.project_row(b,cfg.table_id,cfg.excluded); END IF;
   IF a IS NOT NULL THEN a=db_server_audit.project_row(a,cfg.table_id,cfg.excluded); END IF;
 END IF;
 row_data=COALESCE(a,b); target_id=cfg.table_id;
 IF cfg.kind='field_meta' THEN target_id=row_data->>'table_id'; END IF;
 IF cfg.kind='table_meta' THEN target_id=row_data->>'id'; END IF;
 IF cfg.kind IN ('field_meta','table_meta') AND NOT EXISTS(
   SELECT 1 FROM db_server_audit.targets WHERE kind='record' AND table_id=target_id)
 THEN RETURN NULL; END IF;
 IF TG_OP='UPDATE' AND
    b-ARRAY['__version','__last_modified_time','__last_modified_by'] =
    a-ARRAY['__version','__last_modified_time','__last_modified_by']
 THEN RETURN NULL; END IF;
 INSERT INTO db_server_audit.events(operation,kind,table_id,field_id,record_id,physical_table,actor_hint,before_value,after_value)
 VALUES(TG_OP,cfg.kind,target_id,cfg.field_id,row_data->>cfg.record_key,
 TG_TABLE_SCHEMA||'.'||TG_TABLE_NAME,COALESCE(a->>'__last_modified_by',a->>'last_modified_by',b->>'__last_modified_by'),b,a);
 RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION db_server_audit.capture() FROM PUBLIC;
CREATE OR REPLACE FUNCTION db_server_audit.capture_truncate() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,db_server_audit AS $$
DECLARE cfg db_server_audit.targets%ROWTYPE; projection text;
BEGIN
 SELECT * INTO STRICT cfg FROM db_server_audit.targets WHERE physical=TG_ARGV[0];
 projection=format('to_jsonb(r)-%L::text[]',cfg.excluded);
 IF cfg.kind='record' THEN
   projection=format('db_server_audit.project_row(to_jsonb(r),%L,%L::text[])',cfg.table_id,cfg.excluded);
 END IF;
 EXECUTE format('INSERT INTO db_server_audit.events(operation,kind,table_id,field_id,record_id,physical_table,before_value)
 SELECT ''TRUNCATE_DELETE'',%L,%L,%L,to_jsonb(r)->>%L,%L,%s FROM %I.%I r',
 cfg.kind,cfg.table_id,cfg.field_id,cfg.record_key,TG_TABLE_SCHEMA||'.'||TG_TABLE_NAME,
 projection,TG_TABLE_SCHEMA,TG_TABLE_NAME);
 RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION db_server_audit.capture_truncate() FROM PUBLIC;
'''


def validate(manifest):
    if manifest.get('baseId')!=BASE or len([t for t in manifest['targets'] if t['kind']=='record'])!=7:
        raise ValueError('Expected the approved seven-table base.')
    seen=set()
    for t in manifest['targets']:
        qualified(t['physical']);identifier(t['recordKey'])
        if not t['physical'].startswith(BASE+'.') or t['physical'] in seen:
            raise ValueError('Unrelated or duplicate audit target.')
        seen.add(t['physical'])
        for name in t['exclude']:
            identifier(name)


def install_sql(manifest):
    validate(manifest)
    targets=list(manifest['targets'])+[
        {'physical':'public.field','tableId':'', 'recordKey':'id','kind':'field_meta','fieldId':'','exclude':[]},
        {'physical':'public.table_meta','tableId':'','recordKey':'id','kind':'table_meta','fieldId':'','exclude':[]}]
    statements=['BEGIN;', 'SET LOCAL lock_timeout=\'15s\';', DDL,
        'INSERT INTO db_server_audit.settings VALUES(\'instance\','+literal(uuid.uuid4())+') ON CONFLICT DO NOTHING;']
    # All target writes pause briefly until baseline and triggers commit together.
    statements.extend('LOCK TABLE '+qualified(t['physical'])+' IN SHARE ROW EXCLUSIVE MODE;' for t in targets)
    for t in targets:
        excluded='ARRAY['+','.join(literal(k) for k in t['exclude'])+']::text[]'
        statements.append('INSERT INTO db_server_audit.targets VALUES('+','.join([
            literal(t['physical']),literal(t['tableId']),literal(t['recordKey']),literal(t['kind']),literal(t['fieldId']),excluded])+') '
            'ON CONFLICT(physical) DO UPDATE SET excluded=EXCLUDED.excluded,record_key=EXCLUDED.record_key,kind=EXCLUDED.kind;')
        table=qualified(t['physical'])
        statements.extend(['DROP TRIGGER IF EXISTS db_server_record_changes ON '+table+';',
            'CREATE TRIGGER db_server_record_changes AFTER INSERT OR UPDATE OR DELETE ON '+table+
            ' FOR EACH ROW EXECUTE FUNCTION db_server_audit.capture('+literal(t['physical'])+');'])
        if t['kind'] in ('record','link'):
            statements.extend(['DROP TRIGGER IF EXISTS db_server_record_truncate ON '+table+';',
                'CREATE TRIGGER db_server_record_truncate BEFORE TRUNCATE ON '+table+
                ' FOR EACH STATEMENT EXECUTE FUNCTION db_server_audit.capture_truncate('+literal(t['physical'])+');'])
        where="NOT EXISTS(SELECT 1 FROM db_server_audit.settings WHERE key='baseline_complete')"
        if t['kind']=='field_meta':
            where+=" AND r.table_id IN (SELECT table_id FROM db_server_audit.targets WHERE kind='record')"
        if t['kind']=='table_meta':
            where+=" AND r.id IN (SELECT table_id FROM db_server_audit.targets WHERE kind='record')"
        target_id="r.table_id" if t['kind']=='field_meta' else ('r.id' if t['kind']=='table_meta' else literal(t['tableId']))
        statements.append('INSERT INTO db_server_audit.events(operation,kind,table_id,field_id,record_id,physical_table,after_value) '
            'SELECT \'BASELINE\','+','.join([literal(t['kind']),target_id,literal(t['fieldId']),
            'to_jsonb(r)->>'+literal(t['recordKey']),literal(t['physical']),
            'db_server_audit.project_row(to_jsonb(r),'+literal(t['tableId'])+','+excluded+')' if t['kind']=='record' else 'to_jsonb(r)-'+excluded])+
            ' FROM '+table+' r WHERE '+where+';')
    statements.extend(["INSERT INTO db_server_audit.settings VALUES('baseline_complete','true') ON CONFLICT DO NOTHING;",'COMMIT;'])
    return '\n'.join(statements)


SELFTEST='''BEGIN;
CREATE TEMP TABLE audit_probe(__id text PRIMARY KEY,value text);
INSERT INTO db_server_audit.targets VALUES('__selftest','__selftest','__id','selftest','',ARRAY[]::text[]);
CREATE TRIGGER audit_probe_trigger AFTER INSERT OR UPDATE OR DELETE ON audit_probe
FOR EACH ROW EXECUTE FUNCTION db_server_audit.capture('__selftest');
CREATE TRIGGER audit_probe_truncate BEFORE TRUNCATE ON audit_probe
FOR EACH STATEMENT EXECUTE FUNCTION db_server_audit.capture_truncate('__selftest');
INSERT INTO audit_probe VALUES('probe','before');
UPDATE audit_probe SET value='after' WHERE __id='probe';
DELETE FROM audit_probe WHERE __id='probe';
DO $$ BEGIN
 IF (SELECT count(*) FROM db_server_audit.events WHERE table_id='__selftest')<>3
 OR NOT EXISTS(SELECT 1 FROM db_server_audit.events WHERE table_id='__selftest' AND operation='UPDATE'
   AND before_value->>'value'='before' AND after_value->>'value'='after')
 OR NOT EXISTS(SELECT 1 FROM db_server_audit.events WHERE table_id='__selftest' AND operation='DELETE'
   AND before_value->>'value'='after' AND after_value IS NULL)
 THEN RAISE EXCEPTION 'audit selftest failed'; END IF;
END $$;
INSERT INTO audit_probe VALUES('probe2','truncate');
TRUNCATE audit_probe;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM db_server_audit.events WHERE table_id='__selftest' AND operation='TRUNCATE_DELETE'
   AND before_value->>'value'='truncate') THEN RAISE EXCEPTION 'truncate capture failed'; END IF;
END $$;
ROLLBACK;
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM db_server_audit.events WHERE table_id='__selftest')
 THEN RAISE EXCEPTION 'rollback events survived'; END IF;
END $$;'''


def durable_write(path,data):
    if path.exists():
        if path.read_bytes()!=data:
            raise ValueError('Existing event file differs; backup was not overwritten.')
        return
    missing=[];directory=path.parent
    while not directory.exists():
        missing.append(directory);directory=directory.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        parent_fd=os.open(directory.parent,os.O_DIRECTORY)
        try: os.fsync(parent_fd)
        finally: os.close(parent_fd)
    temporary=path.with_suffix('.partial')
    with temporary.open('wb') as output:
        output.write(data);output.flush();os.fsync(output.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)
    fd=os.open(path.parent,os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def report(values):
    existing=json.loads(STATUS.read_text()) if STATUS.exists() else {}
    existing.update(values)
    STATUS.parent.mkdir(parents=True,exist_ok=True)
    temp=STATUS.with_suffix('.tmp');temp.write_text(json.dumps(existing,indent=2)+'\n')
    temp.chmod(0o644);temp.replace(STATUS)


def export_events():
    mount_check()
    if shutil.disk_usage(DEST.parent).free<1024**3:
        raise RuntimeError('WD backup disk has less than1GiB free.')
    manifest=json.loads(CONFIG.read_text())
    with EXPORT_LOCK.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        instance=sql("SELECT value FROM db_server_audit.settings WHERE key='instance';")
        uuid.UUID(instance)
        exported=0
        while True:
            raw=sql('SELECT row_to_json(x) FROM (SELECT e.* FROM db_server_audit.events e LEFT JOIN '
                'db_server_audit.deliveries d ON d.event_id=e.id WHERE d.event_id IS NULL ORDER BY e.id LIMIT 1000) x;')
            if not raw:
                break
            deliveries=[]
            for line in raw.splitlines():
                event=json.loads(line);event['auditInstance']=instance
                day=dt.datetime.fromisoformat(event['occurred_at']).astimezone(dt.timezone.utc).strftime('%Y/%m/%d')
                filename=DEST/instance/day/(str(event['id']).zfill(20)+'.json')
                data=(json.dumps(event,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode()
                durable_write(filename,data)
                digest=hashlib.sha256(data).hexdigest()
                deliveries.append('('+','.join([str(int(event['id'])),literal(filename.relative_to(DEST)),literal(digest)])+')')
                exported+=1
            # Ack only durable files, by event identity rather than an unsafe high-water ID.
            sql('INSERT INTO db_server_audit.deliveries(event_id,path,sha256) VALUES'+','.join(deliveries)+' ON CONFLICT DO NOTHING;')
        count=int(sql('SELECT count(*) FROM db_server_audit.events;'))
        pending=int(sql('SELECT count(*) FROM db_server_audit.events e LEFT JOIN db_server_audit.deliveries d ON d.event_id=e.id WHERE d.event_id IS NULL;'))
        report({'baseId':manifest['baseId'],'captureReady':True,'tableIds':[t['tableId'] for t in manifest['targets'] if t['kind']=='record'],
            'junctionCount':sum(t['kind']=='link' for t in manifest['targets']),'exportSuccess':True,
            'lastExportAt':dt.datetime.now(dt.timezone.utc).isoformat(),'totalEvents':count,'pendingEvents':pending,'exportedThisRun':exported,
            'backupDirectory':str(DEST),'auditInstance':instance})
        print(json.dumps({'exported':exported,'totalEvents':count,'pending':pending}))


def install(stage,cleanup):
    manifest=json.loads((stage/'record-change-manifest.json').read_text())
    validate(manifest);mount_check()
    CONFIG.parent.mkdir(parents=True,exist_ok=True)
    CONFIG.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n');CONFIG.chmod(0o600)
    sql(install_sql(manifest));sql(SELFTEST)
    shutil.copy2(Path(__file__),'/usr/local/sbin/db-record-changes')
    Path('/usr/local/sbin/db-record-changes').chmod(0o750)
    report({'selfTestPassed':True,'baseId':BASE})
    export_events()
    service='''[Unit]
Description=Export every committed Teable record change to WD
RequiresMountsFor=/srv/db /srv/backups
After=docker.service
[Service]
Type=oneshot
User=root
UMask=0077
ExecStart=/usr/bin/python3 /usr/local/sbin/db-record-changes export
'''
    timer='''[Unit]
Description=Copy durable record journal to WD every15seconds
[Timer]
OnBootSec=30s
OnUnitInactiveSec=15s
AccuracySec=1s
Unit=db-record-changes.service
[Install]
WantedBy=timers.target
'''
    Path('/etc/systemd/system/db-record-changes.service').write_text(service)
    Path('/etc/systemd/system/db-record-changes.timer').write_text(timer)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','enable','--now','db-record-changes.timer'],check=True)
    if cleanup:
        subprocess.run(['runuser','-u','jjj','--','python3',str(stage/'remove-source-fields.py'),'--apply'],check=True)
        export_events()
    print('Transactional capture and WD export installed; root-owned status: '+str(STATUS))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['install','export'])
    p.add_argument('--apply',action='store_true');p.add_argument('--cleanup',action='store_true')
    args=p.parse_args()
    stage=Path(__file__).resolve().parent
    if args.command=='install' and not args.apply:
        manifest=json.loads((stage/'record-change-manifest.json').read_text());validate(manifest)
        print(json.dumps({'plan':'transactional row audit plus WD export','tables':7,
            'junctions':sum(t['kind']=='link' for t in manifest['targets']),
            'cleanupAfterVerifiedBackup':args.cleanup,'WDExportIntervalSeconds':15}))
        return
    if os.geteuid()!=0:
        raise SystemExit('Use sudo for the reviewed installer/exporter.')
    os.umask(0o077)
    if args.command=='install': install(stage,args.cleanup)
    else:
        try: export_events()
        except BaseException:
            report({'exportSuccess':False,'lastFailureAt':dt.datetime.now(dt.timezone.utc).isoformat()})
            raise


if __name__=='__main__':
    main()
