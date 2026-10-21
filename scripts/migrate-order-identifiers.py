#!/usr/bin/env python3
"""Explicit approved VNM/PHL and order-number primary-field migration.

Run on server beside staged order_numbers.py and refresh-order-operations.py.
Default only previews. --apply locks the normal refresher, backs up all seven
tables and SQLite reservations, installs the staged code and marks incomplete
work so cron cannot continue after a partial migration. Re-run to resume.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import sqlite3

ALIASES = {'VIE': 'VNM', 'PHI': 'PHL'}


def prefix(value):
    if not value:
        return value
    match = re.fullmatch(r'(SEA\d{8})(VIE|PHI)', value)
    return match[1] + ALIASES[match[2]] if match else value


def code_values(value):
    if isinstance(value, dict):
        return {k: code_values(v) for k, v in value.items()}
    if isinstance(value, list):
        return [code_values(v) for v in value]
    return ALIASES.get(value, value) if isinstance(value, str) else value


def identity(value):
    if isinstance(value, dict) and 'id' in value:
        return {'id': value['id']}
    if isinstance(value, list):
        return [identity(v) for v in value]
    return value


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--folder', type=Path, default=Path('/home/jjj/db_server_setup/orders-import-20261020'))
    p.add_argument('--apply', action='store_true')
    args = p.parse_args()
    os.umask(0o077)
    folder = args.folder
    with (folder / 'operations.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        helper = runpy.run_path(str(folder / 'import-teable-orders.py'))
        save = helper['save']
        token = Path('/home/jjj/.config/db-server/teable-automation.token')
        api = helper['Api']('http://127.0.0.1:3000', token.read_text().strip())
        state = json.loads((folder / 'state.json').read_text())
        tables = api.call('GET', f'/base/{state["base_id"]}/table')
        if len(tables) != 7 or state.get('structure_version') != 'minimal-7-v1':
            raise ValueError('Expected the approved seven-table structure.')
        backup_path = folder / 'before-identifier-v33-private.json'
        if backup_path.exists():
            backup = json.loads(backup_path.read_text())
        else:
            backup = {'state': json.loads(json.dumps(state)), 'tables': {t['id']: {
                'fields': api.call('GET', f'/table/{t["id"]}/field'),
                'views': api.call('GET', f'/table/{t["id"]}/view'),
                'records': api.records(t['id'])} for t in tables}}
        original = backup['state']
        tid = original['tables']['order_lines']
        old = original['fields']['order_lines']
        primary_id, obsolete = old['__label'], old['order_number']
        old_rows = backup['tables'][tid]['records']
        print(json.dumps({'orders': len(old_rows), 'renumberedCountrySegments': sum(
            r['fields'].get(old['country_code']) in ALIASES and bool(r['fields'].get(obsolete)) for r in old_rows),
            'primary': '订单号', 'missingDatesStayUnnumbered': sum(not r['fields'].get(old['order_date']) for r in old_rows)}, ensure_ascii=False))
        if not args.apply:
            return
        if not backup_path.exists():
            save(backup_path, backup)
            with sqlite3.connect(folder / 'order-counters.sqlite3') as source, sqlite3.connect(folder / 'before-identifier-v33.sqlite3') as dest:
                source.backup(dest)
            for filename in ['order_numbers.py', 'refresh-order-operations.py']:
                shutil.copy2(folder / filename, folder / ('before-identifier-v33-' + filename))
        state['identifier_migration_pending'] = 'v33'
        save(folder / 'state.json', state)
        staged = Path(__file__).resolve().parent
        # Install the pending-marker guard first so a partial code promotion stops cron.
        for filename in ['refresh-order-operations.py', 'order_numbers.py']:
            temp = folder / (filename + '.new')
            shutil.copy2(staged / filename, temp)
            temp.replace(folder / filename)
        rules = runpy.run_path(str(folder / 'order_numbers.py'))
        counter = rules['CountryCounter'](folder / 'order-counters.sqlite3')
        for old_code, new_code in ALIASES.items():
            counter.rename_country(old_code, new_code)
        current = {r['id']: r for r in api.records(tid)}
        if set(current) != {r['id'] for r in old_rows}:
            raise ValueError('Order identities changed since backup; review before resuming.')
        for row in old_rows:
            live = current[row['id']]['fields']
            updates = {}
            for key, expected in [('country_code', ALIASES.get(row['fields'].get(old['country_code']), row['fields'].get(old['country_code']))),
                                  ('number_prefix', prefix(row['fields'].get(old['number_prefix'])))]:
                before = row['fields'].get(old[key])
                if live.get(old[key]) not in (before, expected):
                    raise ValueError('Concurrent identifier edit; migration stopped.')
                if live.get(old[key]) != expected:
                    updates[old[key]] = expected
            if updates:
                api.call('PATCH', f'/table/{tid}/record/{row["id"]}',
                    {'fieldKeyType': 'id', 'typecast': False, 'record': {'fields': updates}})
        # Keep the existing primary field identity so all linked records stay linked.
        fields = api.call('GET', f'/table/{tid}/field')
        template = next(f for f in backup['tables'][tid]['fields'] if f['id'] == obsolete)
        former = next((f for f in fields if f['id'] == obsolete), None)
        if former and former['name'] == '订单号':
            api.call('PATCH', f'/table/{tid}/field/{obsolete}', {'name': '旧订单号（迁移中）'})
        primary = next(f for f in fields if f['id'] == primary_id)
        if primary['type'] != 'formula' or primary['name'] != '订单号':
            api.call('PUT', f'/table/{tid}/field/{primary_id}/convert',
                {'name': '订单号', 'type': 'formula', 'options': template['options']})
        after_rows = api.records(tid)
        actual_numbers = []
        original_rows = {r['id']: r for r in old_rows}
        for row in after_rows:
            before = original_rows[row['id']]['fields']
            old_number = before.get(obsolete)
            expected = prefix(old_number[:-6]) + old_number[-6:] if old_number else None
            if row['fields'].get(primary_id) != expected:
                raise ValueError('Primary order number does not match the preserved date/sequence.')
            if expected:
                actual_numbers.append(expected)
        if len(actual_numbers) != len(set(actual_numbers)):
            raise ValueError('Duplicate primary order numbers; migration stopped.')
        state['fields']['order_lines'].pop('__label', None)
        state['fields']['order_lines']['order_number'] = primary_id
        state['order_primary_version'] = 'order-number-v1'
        state['country_code_version'] = 'vnm-phl-v1'
        save(folder / 'state.json', state)
        if former:
            for table in tables:
                for field in api.call('GET', f'/table/{table["id"]}/field'):
                    if field['id'] == obsolete:
                        continue
                    if obsolete in json.dumps([field.get('options'), field.get('lookupOptions')]):
                        raise ValueError('Obsolete order-number field still referenced; review before deletion.')
            for view in api.call('GET', f'/table/{tid}/view'):
                if obsolete in json.dumps([view.get('filter'), view.get('sort'), view.get('group')]):
                    raise ValueError('Obsolete order-number view dependency requires review.')
            api.call('DELETE', f'/table/{tid}/field/{obsolete}')
        for table in tables:
            for view in api.call('GET', f'/table/{table["id"]}/view'):
                if view.get('filter'):
                    changed = code_values(view['filter'])
                    if changed != view['filter']:
                        api.call('PUT', f'/table/{table["id"]}/view/{view["id"]}/filter', {'filter': changed})
        # No cron may see the intermediate state; all this runs under its lock.
        state.pop('identifier_migration_pending', None)
        save(folder / 'state.json', state)
        op_class = runpy.run_path(str(folder / 'refresh-order-operations.py'))['Operations']
        op = op_class(folder, token)
        try:
            op.refresh()
            final = {t['id']: api.records(t['id']) for t in tables}
            batch = original['tables']['order_batches']
            bf = original['fields']['order_batches']
            ignore = {tid: {primary_id, obsolete, old['country_code'], old['number_prefix'], old['automation_note']},
                      batch: {bf['__label'], bf['batch_number']}}
            for table_id, snapshot in backup['tables'].items():
                current_records = {r['id']: r for r in final[table_id]}
                if set(current_records) != {r['id'] for r in snapshot['records']}:
                    raise ValueError('Record identities changed during migration.')
                for row in snapshot['records']:
                    ignored = ignore.get(table_id, set())
                    before = {k: identity(v) for k,v in row['fields'].items() if k not in ignored and v is not None}
                    after = {k: identity(v) for k,v in current_records[row['id']]['fields'].items() if k not in ignored and v is not None}
                    if before != after:
                        raise ValueError('A protected business value changed: ' + table_id + '/' + row['id'])
            save(folder / 'identifier-v33-result-private.json', {'orders': len(after_rows), 'numbered': len(actual_numbers),
                'countryCodes': list(rules['COUNTRIES'].values()), 'primaryFieldId': primary_id,
                'allRecordIdsAndBusinessValuesPreserved': True})
            print('Verified: order-number primary, canonical codes, stable sequences, record IDs, links and business values.')
        except BaseException:
            state = json.loads((folder / 'state.json').read_text())
            state['identifier_migration_pending'] = 'v33'
            save(folder / 'state.json', state)
            raise


if __name__ == '__main__':
    main()
