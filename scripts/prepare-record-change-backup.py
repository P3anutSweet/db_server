#!/usr/bin/env python3
"""Prepare a credential-free physical-table audit manifest through the local API."""
import json
import os
from pathlib import Path
import runpy


def main():
    os.umask(0o077)
    stage = Path(__file__).resolve().parent
    folder = Path('/home/jjj/db_server_setup/orders-import-20261020')
    helper = runpy.run_path(str(folder / 'import-teable-orders.py'))
    policy = runpy.run_path(str(stage / 'source_cleanup.py'))['retired']
    state = json.loads((folder / 'state.json').read_text())
    api = helper['Api']('http://127.0.0.1:3000', Path('/home/jjj/.config/db-server/teable-automation.token').read_text().strip())
    targets, removals = [], []
    tables = api.call('GET', f'/base/{state["base_id"]}/table')
    if len(tables) != 7:
        raise ValueError('Expected seven approved business tables.')
    for t in tables:
        key = next(k for k,v in state['tables'].items() if v == t['id'])
        reverse = {v:k for k,v in state['fields'][key].items()}
        fields = api.call('GET', f'/table/{t["id"]}/field')
        remove = [f for f in fields if policy(key, reverse.get(f['id']))]
        removals.extend({'table': t['name'], 'field': f['name']} for f in remove)
        targets.append({'physical': t['dbTableName'], 'tableId': t['id'], 'key': key,
            'recordKey': '__id', 'kind': 'record', 'fieldId': '',
            'exclude': [f['dbFieldName'] for f in remove],
            'fields': [{'id':f['id'], 'name':f['name'], 'column':f['dbFieldName'], 'type':f['type'],
                        'options':f.get('options'), 'lookupOptions':f.get('lookupOptions')}
                       for f in fields if f not in remove]})
        for f in fields:
            if f['type'] != 'link' or f in remove:
                continue
            o = f['options']
            if o['fkHostTableName'] == t['dbTableName']:
                continue
            if o['relationship'] != 'manyMany':
                raise ValueError('Unexpected external link storage; audit mapping requires review.')
            targets.append({'physical':o['fkHostTableName'], 'tableId':t['id'], 'key':key,
                'recordKey':o['selfKeyName'], 'kind':'link', 'fieldId':f['id'], 'exclude':[],
                'fields':[], 'foreignKey':o['foreignKeyName'], 'foreignTableId':o['foreignTableId']})
    if len({t['physical'] for t in targets}) != len(targets):
        raise ValueError('Duplicate physical audit targets require explicit mapping.')
    manifest = {'version':1, 'baseId':state['base_id'], 'targets':targets}
    helper['save'](stage / 'record-change-manifest.json', manifest)
    print(json.dumps({'businessTables':len(tables), 'junctions':len(targets)-len(tables),
                      'sourceFieldsToRemove':len(removals), 'removals':removals}, ensure_ascii=False))


if __name__ == '__main__':
    main()
