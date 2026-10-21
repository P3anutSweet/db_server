#!/usr/bin/env python3
"""Remove source/import-only columns after transactional change backup is active."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import runpy
import shutil


def identity(value):
    if isinstance(value, dict) and 'id' in value:
        return {'id':value['id']}
    if isinstance(value, list):
        return [identity(v) for v in value]
    return value


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply', action='store_true')
    args=p.parse_args()
    os.umask(0o077)
    stage=Path(__file__).resolve().parent
    retired=runpy.run_path(str(stage/'source_cleanup.py'))['retired']
    folder=Path('/home/jjj/db_server_setup/orders-import-20261020')
    helper=runpy.run_path(str(folder/'import-teable-orders.py'))
    save=helper['save']
    token=Path('/home/jjj/.config/db-server/teable-automation.token')
    api=helper['Api']('http://127.0.0.1:3000',token.read_text().strip())
    with (folder/'operations.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        state=json.loads((folder/'state.json').read_text())
        tables=api.call('GET',f'/base/{state["base_id"]}/table')
        if len(tables)!=7 or state.get('order_primary_version')!='order-number-v1':
            raise ValueError('Expected the approved V3.3 seven-table database.')
        backup_path=folder/'source-cleanup-business-baseline-private.json'
        snapshots={}
        remove={}
        for key,tid in state['tables'].items():
            fields=api.call('GET',f'/table/{tid}/field')
            reverse={v:k for k,v in state['fields'][key].items()}
            remove[tid]=[f for f in fields if retired(key,reverse.get(f['id']))]
            excluded={f['id'] for f in remove[tid]}
            snapshots[tid]={'fields':fields,'views':api.call('GET',f'/table/{tid}/view'),
                'records':[{'id':r['id'],'fields':{fid:identity(v) for fid,v in r['fields'].items() if fid not in excluded and v is not None}}
                           for r in api.records(tid)]}
        retired_ids={f['id'] for fields in remove.values() for f in fields}
        for tid,data in snapshots.items():
            for field in data['fields']:
                if field['id'] in retired_ids:
                    continue
                refs=json.dumps([field.get('options'),field.get('lookupOptions')])
                if any(fid in refs for fid in retired_ids):
                    raise ValueError('Source column is still used by a current business field.')
            for view in data['views']:
                if view['name']=='原始资料':
                    continue
                refs=json.dumps([view.get('filter'),view.get('sort'),view.get('group')])
                if any(fid in refs for fid in retired_ids):
                    raise ValueError('Source column is still used by a business view filter/sort.')
        print(json.dumps({'fieldsToRemove':len(retired_ids),'tables':len(tables)},ensure_ascii=False))
        if not args.apply:
            return
        if state.get('source_storage_version')=='removed-v1' and not state.get('source_cleanup_pending') and not retired_ids:
            print('Source fields are already removed; current business edits are preserved.')
            return
        status_path=Path('/var/lib/db-server/record-change-status.json')
        if status_path.stat().st_uid!=0 or status_path.stat().st_mode & 0o022:
            raise ValueError('Expected root-owned audit status.')
        status=json.loads(status_path.read_text())
        if not status.get('captureReady') or not status.get('selfTestPassed') or not status.get('exportSuccess') or status.get('baseId')!=state['base_id']:
            raise ValueError('Transactional capture and off-disk baseline backup must pass first.')
        watched=set(status['tableIds'])
        if watched!=set(state['tables'].values()):
            raise ValueError('Change capture does not cover all seven tables.')
        if not backup_path.exists():
            # No source cell values are copied into this new baseline.
            save(backup_path,{'tables':snapshots,'removedFieldIds':{tid:[f['id'] for f in fields] for tid,fields in remove.items()}})
        else:
            snapshots=json.loads(backup_path.read_text())['tables']
        approved=json.loads(backup_path.read_text())['removedFieldIds']
        old_code=folder/'before-source-cleanup-refresh-order-operations.py'
        if not old_code.exists():
            shutil.copy2(folder/'refresh-order-operations.py',old_code)
        temp=folder/'refresh-order-operations.py.new'
        shutil.copy2(stage/'refresh-order-operations.py',temp)
        temp.replace(folder/'refresh-order-operations.py')
        state['source_cleanup_pending']=True
        save(folder/'state.json',state)
        for key,tid in state['tables'].items():
            for view in api.call('GET',f'/table/{tid}/view'):
                if view['name']=='原始资料':
                    api.call('DELETE',f'/table/{tid}/view/{view["id"]}')
                elif key=='order_lines' and view['name']=='全部订单台账':
                    api.call('PUT',f'/table/{tid}/view/{view["id"]}/name',{'name':'全部订单'})
            for field in remove[tid]:
                api.call('DELETE',f'/table/{tid}/field/{field["id"]}')
                mapping=state['fields'][key]
                for logical in [k for k,v in mapping.items() if v==field['id']]:
                    del mapping[logical]
                save(folder/'state.json',state)
        state['source_storage_version']='removed-v1'
        state.pop('source_cleanup_pending',None)
        save(folder/'state.json',state)
        op=runpy.run_path(str(folder/'refresh-order-operations.py'))['Operations'](folder,token)
        try:
            op.refresh()
            for tid,data in snapshots.items():
                excluded=set(approved[tid])
                current_ids={f['id'] for f in api.call('GET',f'/table/{tid}/field')}
                old_ids={f['id'] for f in data['fields']}
                if not old_ids-current_ids <= excluded:
                    raise ValueError('An unapproved business field was removed during cleanup.')
                before={r['id']:{k:v for k,v in r['fields'].items() if k not in excluded} for r in data['records']}
                after={r['id']:{k:identity(v) for k,v in r['fields'].items() if k not in excluded and v is not None} for r in api.records(tid)}
                if before!=after:
                    raise ValueError('Protected business values changed during cleanup: '+tid)
            save(folder/'source-cleanup-result-private.json',{'businessRecordsPreserved':True,'sourceFieldsRemoved':sum(len(v) for v in approved.values())})
            print('Source fields removed; all current record IDs, links and business values verified unchanged.')
        except BaseException:
            state=json.loads((folder/'state.json').read_text());state['source_cleanup_pending']=True
            save(folder/'state.json',state)
            raise


if __name__=='__main__':
    main()
