"""Run audit SQL against a disposable unprivileged PostgreSQL cluster, never production."""
import argparse
import json
import os
from pathlib import Path
import runpy
import subprocess


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pg-bin',type=Path,required=True);p.add_argument('--share-dir',type=Path,required=True)
    p.add_argument('--work',type=Path,required=True);p.add_argument('--program',type=Path,required=True)
    args=p.parse_args()
    work=args.work.resolve();work.mkdir(mode=0o700,parents=True,exist_ok=True)
    data=work/'data';sock=work/'sock';sock.mkdir(mode=0o700,exist_ok=True)
    if data.exists(): raise ValueError('Use a new disposable cluster directory.')
    env=os.environ.copy()
    env['LD_LIBRARY_PATH']=str(args.pg_bin.parents[3]/'lib/x86_64-linux-gnu')
    def execute(name,*argv):
        r=subprocess.run([str(args.pg_bin/name),*map(str,argv)],env=env,text=True,capture_output=True)
        if r.returncode: raise RuntimeError(name+': '+r.stderr)
        return r.stdout
    def sql(text):
        r=subprocess.run([str(args.pg_bin/'psql'),'-X','-qAt','-h',str(sock),'-p','55432','-d','postgres','-v','ON_ERROR_STOP=1'],
            input=text,env=env,text=True,capture_output=True)
        if r.returncode: raise RuntimeError(r.stderr)
        return r.stdout.strip()
    execute('initdb','-D',data,'-L',args.share_dir,'--locale=C','--encoding=UTF8','-A','trust')
    started=False
    try:
        execute('pg_ctl','-D',data,'-l',work/'postgres.log','-o',"-h '' -k "+str(sock)+' -p 55432','-w','start');started=True
        m=runpy.run_path(str(args.program));base=m['BASE']
        q=m['qualified'];lit=m['literal']
        sql('CREATE SCHEMA '+m['identifier'](base)+''';
CREATE TABLE public.field(id text PRIMARY KEY,name text,table_id text,db_field_name text,deleted_time timestamptz);
CREATE TABLE public.table_meta(id text PRIMARY KEY,name text,base_id text,deleted_time timestamptz);''')
        targets=[]
        for i in range(7):
            physical=base+'.test_'+str(i);tid='tbltest'+str(i)
            sql('CREATE TABLE '+q(physical)+'(__id text PRIMARY KEY,value text,raw_source text,__version integer DEFAULT0);'.replace('DEFAULT0','DEFAULT 0'))
            sql('INSERT INTO public.table_meta VALUES('+','.join([lit(tid),lit('test'),lit(base),'NULL'])+');'
                'INSERT INTO public.field VALUES('+','.join([lit('fld'+str(i)),lit('value'),lit(tid),lit('value'),'NULL'])+');'
                'INSERT INTO '+q(physical)+" VALUES('baseline','business','private-original',0);")
            targets.append({'physical':physical,'tableId':tid,'recordKey':'__id','kind':'record','fieldId':'','exclude':['raw_source']})
        link=base+'.junction_test'
        sql('CREATE TABLE '+q(link)+'(owner_id text,foreign_id text);')
        targets.append({'physical':link,'tableId':'tbltest0','recordKey':'owner_id','kind':'link','fieldId':'fldlink','exclude':[]})
        manifest={'baseId':base,'targets':targets}
        sql(m['install_sql'](manifest));sql(m['SELFTEST'])
        initial=sql('SELECT count(*) FROM db_server_audit.events;')
        sql(m['install_sql'](manifest))
        assert sql('SELECT count(*) FROM db_server_audit.events;')==initial
        table=q(targets[0]['physical'])
        sql('INSERT INTO '+table+" VALUES('rapid','A','raw',0); UPDATE "+table+" SET value='B' WHERE __id='rapid'; UPDATE "+table+" SET value='C' WHERE __id='rapid'; DELETE FROM "+table+" WHERE __id='rapid';")
        changes=[json.loads(x) for x in sql("SELECT row_to_json(x) FROM (SELECT operation,before_value,after_value FROM db_server_audit.events WHERE record_id='rapid' ORDER BY id) x;").splitlines()]
        assert [x['operation'] for x in changes]==['INSERT','UPDATE','UPDATE','DELETE']
        assert [x['after_value']['value'] for x in changes[:3]]==['A','B','C']
        assert changes[-1]['before_value']['value']=='C'
        assert 'raw_source' not in json.dumps(changes)
        before=sql('SELECT count(*) FROM db_server_audit.events;')
        sql('BEGIN; UPDATE '+table+" SET value='rollback' WHERE __id='baseline'; ROLLBACK;")
        assert sql('SELECT count(*) FROM db_server_audit.events;')==before
        sql('INSERT INTO '+q(link)+" VALUES('baseline','foreign'); DELETE FROM "+q(link)+" WHERE owner_id='baseline';")
        assert sql("SELECT count(*) FROM db_server_audit.events WHERE kind='link';")=='2'
        sql('ALTER TABLE '+table+' RENAME COLUMN raw_source TO orphaned_source; ALTER TABLE '+table+' ADD COLUMN new_business text;'+
            "INSERT INTO public.field VALUES('fld_new','new','tbltest0','new_business',NULL); UPDATE "+table+" SET new_business='new' WHERE __id='baseline';")
        last=json.loads(sql("SELECT after_value FROM db_server_audit.events WHERE kind='record' ORDER BY id DESC LIMIT1;".replace('LIMIT1','LIMIT 1')))
        assert last['new_business']=='new' and 'orphaned_source' not in last
        sql('TRUNCATE '+table+';')
        last=json.loads(sql("SELECT before_value FROM db_server_audit.events WHERE operation='TRUNCATE_DELETE' ORDER BY id DESC LIMIT1;".replace('LIMIT1','LIMIT 1')))
        assert last['value']=='business' and 'orphaned_source' not in last
        assert not sql("SELECT id FROM db_server_audit.events WHERE table_id='__selftest';")
        # Exercise real disk writes and database delivery acknowledgements.
        ns=m['export_events'].__globals__
        config=work/'manifest.json';config.write_text(json.dumps(manifest))
        ns.update({'sql':sql,'mount_check':lambda:None,'CONFIG':config,
                   'STATUS':work/'status.json','DEST':work/'backups','EXPORT_LOCK':work/'export.lock'})
        def crash_before_ack(statement):
            if statement.startswith('INSERT INTO db_server_audit.deliveries'):
                raise RuntimeError('simulated crash before acknowledgement')
            return sql(statement)
        ns['sql']=crash_before_ack
        try: m['export_events']()
        except RuntimeError as e: assert str(e)=='simulated crash before acknowledgement'
        else: raise AssertionError('Crash simulation did not run')
        assert list((work/'backups').rglob('*.json'))
        assert sql('SELECT count(*) FROM db_server_audit.deliveries;')=='0'
        ns['sql']=sql;m['export_events']()
        assert sql('SELECT count(*) FROM db_server_audit.events;')==sql('SELECT count(*) FROM db_server_audit.deliveries;')
        # Sequence IDs are allocated before commit. A late commit must never be skipped.
        connection=subprocess.Popen([str(args.pg_bin/'psql'),'-X','-qAt','-h',str(sock),'-p','55432','-d','postgres','-v','ON_ERROR_STOP=1'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
        try:
            connection.stdin.write("BEGIN; INSERT INTO db_server_audit.events(operation,kind,table_id,record_id,physical_table) VALUES('INSERT','test','tbltest0','late','test') RETURNING id;\n\\echo READY\n")
            connection.stdin.flush()
            late=int(connection.stdout.readline());assert connection.stdout.readline().strip()=='READY'
            early=int(sql("INSERT INTO db_server_audit.events(operation,kind,table_id,record_id,physical_table) VALUES('INSERT','test','tbltest0','early','test') RETURNING id;"))
            assert late<early
            m['export_events']()
            assert sql('SELECT count(*) FROM db_server_audit.deliveries WHERE event_id='+str(early))=='1'
            connection.stdin.write('COMMIT;\n\\q\n');connection.stdin.flush();connection.wait(timeout=15)
            m['export_events']()
            assert sql('SELECT count(*) FROM db_server_audit.deliveries WHERE event_id='+str(late))=='1'
        finally:
            if connection.poll() is None: connection.terminate();connection.wait(timeout=15)
        before=sql('SELECT count(*) FROM db_server_audit.deliveries;');m['export_events']()
        assert sql('SELECT count(*) FROM db_server_audit.deliveries;')==before
        print('PASS: baseline/idempotence; rapid edits/deletes; rollback; links; source exclusion; new fields; TRUNCATE; durable export; crash before ack; late commit; duplicate-free rerun.')
    finally:
        if started: execute('pg_ctl','-D',data,'-m','fast','-w','stop')


if __name__=='__main__': main()
