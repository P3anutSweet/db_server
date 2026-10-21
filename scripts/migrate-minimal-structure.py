#!/usr/bin/env python3
"""Consolidate the approved thirteen-table model to seven tables, preserving history.

No changes without --apply. Source records are copied and verified before recoverable
table retirement. A durable private snapshot makes an interrupted migration resumable.
"""
import argparse
from collections import defaultdict
import copy
import datetime as dt
import json
import os
from pathlib import Path
import runpy
import sys

KEEP = ('monthly_overview', 'order_lines', 'order_batches', 'skus', 'customers', 'markets', 'tags')
RETIRE = ('customer_preferences', 'product_tags', 'source_rows', 'line_plans', 'import_batches', 'regions')
VERSION = 'minimal-7-v1'


def linked(value):
    if not value:
        return []
    return [v['id'] for v in (value if isinstance(value, list) else [value])]


def logical(snapshot, table, row):
    return {key: copy.deepcopy(row['fields'].get(fid)) for key, fid in snapshot['state']['fields'][table].items()}


def prepare(snapshot):
    """Build lossless order histories and validate retirement preconditions offline."""
    tables = snapshot['tables']
    if not set(KEEP + RETIRE) <= set(tables):
        raise ValueError('The original thirteen-table snapshot is incomplete.')
    for key in ('customer_preferences', 'product_tags'):
        if tables[key]['records']:
            raise ValueError('Tag relation records were added; migrate their per-tag metadata before retiring these tables.')
    byid = {key: {r['id']: r for r in t['records']} for key, t in tables.items()}
    plan_map = defaultdict(list)
    for row in tables['line_plans']['records']:
        values = logical(snapshot, 'line_plans', row)
        targets = linked(values['line'])
        if len(targets) != 1 or targets[0] not in byid['order_lines']:
            raise ValueError('A reference plan has no unambiguous existing order.')
        if values['state'] != '草稿' or values['plan_kind'] != '原计划节点待核对' or not str(values['__key']).startswith('source-plan:'):
            raise ValueError('A plan now contains operational business data; review its migration before retirement.')
        plan_map[targets[0]].append({'record_id': row['id'], 'values': values})
    histories, used_sources = {}, set()
    for row in tables['order_lines']['records']:
        values = logical(snapshot, 'order_lines', row)
        source_ids = linked(values['source_rows'])
        sources = []
        for source_id in source_ids:
            if source_id not in byid['source_rows']:
                raise ValueError('Order links to a missing source row.')
            source = logical(snapshot, 'source_rows', byid['source_rows'][source_id])
            snapshots = []
            for sid in linked(source['snapshot']):
                if sid not in byid['import_batches']:
                    raise ValueError('Source snapshot metadata is missing.')
                snapshots.append({'record_id': sid, 'values': logical(snapshot, 'import_batches', byid['import_batches'][sid])})
            sources.append({'record_id': source_id, 'values': source, 'import_snapshots': snapshots})
            used_sources.add(source_id)
        histories[row['id']] = {
            'source_provenance': json.dumps({'format': 'source-history-v1', 'rows': sources}, ensure_ascii=False, sort_keys=True),
            'source_plan_history': json.dumps({'format': 'reference-plans-v1', 'plans': plan_map[row['id']]}, ensure_ascii=False, sort_keys=True)}
    if used_sources != set(byid['source_rows']):
        raise ValueError('An original source row would be orphaned; retain its table.')
    used_snapshots = {sid for row in tables['source_rows']['records']
        for sid in linked(logical(snapshot, 'source_rows', row)['snapshot'])}
    if used_snapshots != set(byid['import_batches']):
        raise ValueError('Unreferenced import metadata requires an explicit archive owner.')
    markets = {}
    for row in tables['markets']['records']:
        values = logical(snapshot, 'markets', row)
        parents = linked(values['region'])
        if len(parents) != 1 or parents[0] not in byid['regions']:
            raise ValueError('A market has no unambiguous business region.')
        parent = logical(snapshot, 'regions', byid['regions'][parents[0]])
        if not parent['code'] or not parent['name']:
            raise ValueError('Business region name/code is missing.')
        markets[row['id']] = {'region': parent['name'], 'business_region_code': parent['code'],
            'region_provenance': json.dumps({'record_id': parents[0], 'values': parent}, ensure_ascii=False, sort_keys=True)}
    if {v['region'] for v in markets.values()} != {logical(snapshot, 'regions', r)['name'] for r in tables['regions']['records']}:
        raise ValueError('An unreferenced region needs an archive owner before retirement.')
    return {'histories': histories, 'markets': markets}


def capture(op):
    reverse = {tid: key for key, tid in op.tables.items()}
    tables = {}
    for t in op.catalog:
        if t['id'] not in reverse:
            raise ValueError('An unrecognized table was added; review before consolidation.')
        key = reverse[t['id']]
        tables[key] = {'table': t, 'records': op.api.records(t['id']),
            'fields': op.api.call('GET', f'/table/{t["id"]}/field'),
            'views': op.api.call('GET', f'/table/{t["id"]}/view')}
    return {'at': dt.datetime.now(dt.timezone.utc).isoformat(), 'state': copy.deepcopy(op.state), 'tables': tables}


def verify_business(op, before):
    """Check every pre-existing retained cell, excluding only intentionally folded links."""
    excluded = {'order_lines': {'source_rows'}, 'order_batches': {'source_rows'}, 'markets': {'region'}}
    for table in KEEP:
        current = {r['id']: r for r in op.rows(table)}
        original = before['tables'][table]['records']
        if set(current) != {r['id'] for r in original}:
            raise ValueError('Record identities changed in ' + table)
        for old in original:
            for key, fid in before['state']['fields'][table].items():
                if key in excluded.get(table, set()):
                    continue
                if current[old['id']]['fields'].get(fid) != old['fields'].get(fid):
                    raise ValueError('A pre-existing business value changed in ' + table + '.' + key)


def apply(op):
    if op.state.get('structure_version') == VERSION and op.state.get('minimal_migration', {}).get('phase') == 'complete':
        op.validate_minimal()
        print('Seven-table structure already applied; no migration writes.')
        return
    archive = op.folder / 'minimal-7-backup-private.json'
    if archive.exists():
        snapshot = json.loads(archive.read_text())
        if snapshot['state']['base_id'] != op.state['base_id']:
            raise ValueError('Snapshot belongs to another base.')
    else:
        snapshot = capture(op)
        prepare(snapshot)
        op.save(archive, snapshot)
    plan = prepare(snapshot)
    op.state['minimal_migration'] = {'version': VERSION, 'phase': 'copying', 'backup': archive.name}
    op.persist()
    op.field('order_lines', 'source_provenance', '原始导入资料', 'longText')
    op.field('order_lines', 'source_plan_history', '原交付计划资料', 'longText')
    op.field('markets', 'business_region_code', '业务大区编码')
    op.field('markets', 'region_provenance', '原大区资料', 'longText')
    op.link('customers', 'preferred_tags', '偏好标签', 'tags', True)
    op.field('customers', 'preference_notes', '偏好说明', 'longText')
    op.link('skus', 'tags', '产品标签', 'tags', True)
    op.field('skus', 'tag_notes', '标签依据及说明', 'longText')
    op.field('skus', 'ean_code', 'EAN')
    orders = {r['id']: r for r in op.rows('order_lines')}
    for rid, values in plan['histories'].items():
        op.patch('order_lines', orders[rid], values)
    market_fields = op.api.call('GET', f'/table/{op.tables["markets"]}/field')
    region = next(f for f in market_fields if f['id'] == op.fields['markets']['region'])
    names = sorted({v['region'] for v in plan['markets'].values()})
    if region['type'] == 'link':
        op.api.call('PUT', f'/table/{op.tables["markets"]}/field/{region["id"]}/convert', {
            'name': '业务大区', 'type': 'singleSelect', 'options': {'choices': [{'name': n} for n in names]}})
    elif region['type'] != 'singleSelect':
        raise ValueError('Unexpected market region field type.')
    for row in op.rows('markets'):
        op.patch('markets', row, plan['markets'][row['id']])
    verify_business(op, snapshot)
    check_orders = {r['id']: r for r in op.rows('order_lines')}
    for rid, values in plan['histories'].items():
        for key, expected in values.items():
            if op.get(check_orders[rid], 'order_lines', key) != expected:
                raise ValueError('Embedded source/plan history failed read-back.')
    check_markets = {r['id']: r for r in op.rows('markets')}
    for rid, values in plan['markets'].items():
        for key, expected in values.items():
            if op.get(check_markets[rid], 'markets', key) != expected:
                raise ValueError('Inline market region failed read-back.')
    # Before discarding originals, compare every still-active retiring record with
    # the snapshot. Concurrent human edits stop retirement instead of being lost.
    live_ids = {t['id'] for t in op.api.call('GET', f'/base/{op.state["base_id"]}/table')}
    for table in RETIRE:
        tid = snapshot['state']['tables'][table]
        if tid in live_ids:
            old = {r['id']: r['fields'] for r in snapshot['tables'][table]['records']}
            actual = {r['id']: r['fields'] for r in op.api.records(tid)}
            # Region conversion only affects the market side of the old relationship.
            if actual != old:
                raise ValueError('Source table changed during migration: ' + table)
    op.state['minimal_migration']['phase'] = 'copied_and_verified'
    op.persist()
    # Drop obsolete retained-side links only after the full information is embedded.
    for table in ('order_lines', 'order_batches'):
        fid = op.fields[table].get('source_rows')
        if fid:
            actual = {f['id'] for f in op.api.call('GET', f'/table/{op.tables[table]}/field')}
            if fid in actual:
                op.api.call('DELETE', f'/table/{op.tables[table]}/field/{fid}')
            op.state.setdefault('retired_field_ids', {}).setdefault(table, {})['source_rows'] = op.fields[table].pop('source_rows')
            op.persist()
    for table in RETIRE:
        tid = op.tables.get(table) or op.state.get('removed_tables', {}).get(table)
        live_ids = {t['id'] for t in op.api.call('GET', f'/base/{op.state["base_id"]}/table')}
        if tid in live_ids:
            op.api.call('DELETE', f'/base/{op.state["base_id"]}/table/{tid}')
        if table in op.tables:
            op.state.setdefault('removed_tables', {})[table] = op.tables.pop(table)
            op.state.setdefault('removed_fields', {})[table] = op.fields.pop(table)
        op.persist()
    active = op.api.call('GET', f'/base/{op.state["base_id"]}/table')
    if {t['id'] for t in active} != {op.tables[k] for k in KEEP}:
        raise ValueError('Unexpected active tables after consolidation.')
    op.active = {t['id'] for t in active}
    op.state['structure_version'] = VERSION
    op.state['minimal_migration']['phase'] = 'retired_pending_layout'
    op.persist()
    batch_id = op.tables['order_batches']
    if next(t for t in active if t['id'] == batch_id)['name'] != '订单批次':
        op.api.call('PUT', f'/base/{op.state["base_id"]}/table/{batch_id}/name', {'name': '订单批次'})
    op.api.call('PATCH', f'/table/{op.tables["order_lines"]}/field/{op.fields["order_lines"]["batch"]}', {'name': '订单批次'})
    for left, right in zip(KEEP, KEEP[1:]):
        op.api.call('PUT', f'/base/{op.state["base_id"]}/table/{op.tables[right]}/order',
                    {'anchorId': op.tables[left], 'position': 'after'})
    # Move the first daily entry ahead of all other retained tables.
    ordered = op.api.call('GET', f'/base/{op.state["base_id"]}/table')
    first = min(ordered, key=lambda t: t['order'])['id']
    if first != op.tables[KEEP[0]]:
        op.api.call('PUT', f'/base/{op.state["base_id"]}/table/{op.tables[KEEP[0]]}/order',
                    {'anchorId': first, 'position': 'before'})
    op.layouts()
    op.state['operations_layout_version'] = '3.1'
    op.state['current_month_filter'] = dt.datetime.now(__import__('zoneinfo').ZoneInfo('Asia/Shanghai')).strftime('%Y-%m')
    op.persist()
    verify_business(op, snapshot)
    op.state['minimal_migration']['phase'] = 'complete'
    op.persist()
    op.save(op.folder / 'minimal-7-result-private.json', {
        'at': dt.datetime.now(dt.timezone.utc).isoformat(), 'version': VERSION,
        'active_tables': 7, 'preserved_orders': len(orders), 'embedded_source_rows': len(snapshot['tables']['source_rows']['records']),
        'embedded_reference_plans': len(snapshot['tables']['line_plans']['records']), 'retired_tables': list(RETIRE)})
    print('Seven-table migration complete; histories and business values verified.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, required=True)
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({'mode': 'plan_only', 'keep': KEEP, 'retire_after_verified_copy': RETIRE}))
        return
    import fcntl
    os.umask(0o077)
    with (args.folder / 'operations.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        Operations = runpy.run_path(str(args.folder / 'refresh-order-operations.py'))['Operations']
        op = Operations(args.folder, args.token_file)
        apply(op)
        op.refresh()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(type(error).__name__ + ': ' + str(error), file=sys.stderr)
        raise SystemExit(1)
