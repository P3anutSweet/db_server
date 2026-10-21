#!/usr/bin/env python3
"""Apply a private, user-confirmed market directory through the Teable API.

No accounts, permissions or historical order/customer assignments are created.
Names and customer records belong in a private data file, never in this script or Git.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import runpy
import sys
import time


def apply(api, state, data, save_state):
    if state.get('structure_version') == 'minimal-7-v1':
        raise ValueError('The region directory table was retired. Maintain market owners and customer records in the seven-table structure.')
    tables, fields = state['tables'], state['fields']

    def ensure_field(table, ro):
        tid = tables[table]
        existing = {f['name']: f for f in api.call('GET', f'/table/{tid}/field')}
        result = existing.get(ro['name'])
        if result is None:
            result = api.call('POST', f'/table/{tid}/field', ro)
        if result['type'] != ro['type'] or bool(result.get('isLookup')) != bool(ro.get('isLookup')):
            raise ValueError('Existing field does not match expected directory type.')
        if ro.get('isLookup'):
            for key in ['foreignTableId', 'linkFieldId', 'lookupFieldId']:
                if result['lookupOptions'][key] != ro['lookupOptions'][key]:
                    raise ValueError('Existing owner lookup points to a different source.')
        fields[table]['business_owner'] = result['id']
        save_state()
        return result['id']

    def patch(table, rid, values):
        return api.call('PATCH', f'/table/{tables[table]}/record/{rid}', {
            'fieldKeyType': 'id', 'typecast': False,
            'record': {'fields': {fields[table][k]: v for k, v in values.items()}}})

    # Confirm targets and required existing records before making any changes.
    market_rows = {r['fields'].get(fields['markets']['name']): r for r in api.records(tables['markets'])}
    region_rows = {r['fields'].get(fields['regions']['code']): r for r in api.records(tables['regions'])}
    if not set(data['market_owners']) <= set(market_rows):
        raise ValueError('A named market is missing; no new market is invented.')
    if not set(data['region_owners']) <= set(region_rows):
        raise ValueError('A named region is missing.')
    for c in data['customers']:
        if c['market'] not in market_rows:
            raise ValueError('Customer market is unknown.')
    protected = [fields['order_lines'][k] for k in ['customer', 'source_quantity', 'source_picked', 'source_pending']]
    baseline = {r['id']: {k: r['fields'].get(k) for k in protected}
                for r in api.records(tables['order_lines'])}

    text_ro = {'name': '业务负责人', 'type': 'singleLineText', 'options': {},
               'description': '用户确认的业务负责人姓名；不等同Teable账号或访问权限'}
    ensure_field('regions', text_ro)
    ensure_field('markets', text_ro)
    for code, name in data['region_owners'].items():
        patch('regions', region_rows[code]['id'], {'business_owner': name})
    for market, owner in data['market_owners'].items():
        patch('markets', market_rows[market]['id'], {'business_owner': owner})
    for market, code in data.get('confirmed_region_membership', {}).items():
        patch('markets', market_rows[market]['id'], {'region': {'id': region_rows[code]['id']},
                                                  'review_state': '用户已确认'})

    for table, link in [('customers', 'markets'), ('order_lines', 'market')]:
        ensure_field(table, {'name': '业务负责人', 'type': 'singleLineText', 'isLookup': True,
            'description': '从关联地区市场带出的业务负责人；同一客户有多个市场时显示对应负责人',
            'options': {}, 'lookupOptions': {'foreignTableId': tables['markets'],
                'linkFieldId': fields[table][link], 'lookupFieldId': fields['markets']['business_owner']}})

    def linked_ids(value):
        return {v['id'] for v in (value if isinstance(value, list) else [value] if value else [])}

    for customer in data['customers']:
        current = api.records(tables['customers'])
        name, market = customer['name'], customer['market']
        rid_market = market_rows[market]['id']
        key = 'directory:customer:' + market + ':' + name.casefold()
        matches = [r for r in current if r['fields'].get(fields['customers']['__key']) == key or
                   (str(r['fields'].get(fields['customers']['name'], '')).casefold() == name.casefold()
                    and rid_market in linked_ids(r['fields'].get(fields['customers']['markets'])))]
        if len(matches) > 1:
            raise ValueError('Ambiguous customer identity; manual merge required.')
        if not matches:
            values = {'__label': name, '__key': key, 'code': customer['code'], 'name': name,
                      'markets': [{'id': rid_market}], 'notes': '用户确认的客户；历史订单对应关系尚未核对'}
            api.call('POST', f'/table/{tables["customers"]}/record', {'fieldKeyType': 'id', 'typecast': False,
                'records': [{'fields': {fields['customers'][k]: v for k, v in values.items()}}]})

    def layout(table, shown):
        tid = tables[table]
        for view in api.call('GET', f'/table/{tid}/view'):
            if table == 'order_lines':
                changes = [{'fieldId': fields[table]['business_owner'],
                            'columnMeta': {'order': 1.5, 'hidden': False, 'width': 105}}]
            else:
                changes = [{'fieldId': fid, 'columnMeta': {
                    'order': shown.index(key) if key in shown else 100 + i,
                    'hidden': key not in shown, 'width': 160}}
                    for i, (key, fid) in enumerate(fields[table].items())]
            api.call('PUT', f'/table/{tid}/view/{view["id"]}/column-meta', changes)

    layout('regions', ['__label', 'business_owner', 'active'])
    layout('markets', ['__label', 'region', 'business_owner', 'review_state'])
    layout('customers', ['__label', 'markets', 'business_owner', 'state', 'notes'])
    layout('order_lines', [])

    # Read back actual derived values as well as written names; tolerate short lookup propagation.
    report = None
    for attempt in range(4):
        owner_counts = Counter()
        orders = api.records(tables['order_lines'])
        check_markets = api.records(tables['markets'])
        check_customers = api.records(tables['customers'])
        if baseline != {r['id']: {k: r['fields'].get(k) for k in protected} for r in orders}:
            raise ValueError('Historical customer/quantity values changed during the directory update; review before continuing.')
        check_regions = {r['fields'].get(fields['regions']['code']): r for r in api.records(tables['regions'])}
        markets_by_id = {r['id']: r for r in check_markets}
        good = True
        for code, expected in data['region_owners'].items():
            good &= check_regions[code]['fields'].get(fields['regions']['business_owner']) == expected
        for r in check_markets:
            name = r['fields'].get(fields['markets']['name'])
            if name in data['market_owners']:
                good &= r['fields'].get(fields['markets']['business_owner']) == data['market_owners'][name]
        for r in orders:
            market_ids = linked_ids(r['fields'].get(fields['order_lines']['market']))
            if len(market_ids) != 1:
                raise ValueError('Order does not have exactly one market.')
            market = markets_by_id[next(iter(market_ids))]['fields'][fields['markets']['name']]
            expected = data['market_owners'].get(market)
            value = r['fields'].get(fields['order_lines']['business_owner'])
            value = value[0] if isinstance(value, list) and len(value) == 1 else value
            good &= value == expected
            if value:
                owner_counts[str(value)] += 1
        customer_checks = []
        for c in data['customers']:
            rows = [r for r in check_customers if r['fields'].get(fields['customers']['name']) == c['name']
                    and market_rows[c['market']]['id'] in linked_ids(r['fields'].get(fields['customers']['markets']))]
            if len(rows) != 1:
                raise ValueError('Customer read-back mismatch or duplicate.')
            value = rows[0]['fields'].get(fields['customers']['business_owner'])
            value = value[0] if isinstance(value, list) and len(value) == 1 else value
            good &= value == data['market_owners'][c['market']]
            customer_checks.append({'name': c['name'], 'market': c['market'], 'owner': value})
        if good:
            report = {'markets_updated': len(data['market_owners']), 'customers': customer_checks,
                      'order_owner_counts': dict(owner_counts), 'order_count': len(orders),
                      'historical_customer_assignments_changed': False}
            break
        if attempt < 3:
            time.sleep(0.5)
    if report is None:
        raise ValueError('Owner lookups did not reconcile; review field calculation status.')
    state['directory_update_report'] = report
    save_state()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--data', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    data = json.loads(args.data.read_text(encoding='utf-8'))
    if not args.apply:
        print(json.dumps({'action': 'offline_plan_only', 'markets': len(data['market_owners']),
                          'customers': len(data['customers'])}))
        return
    helper = runpy.run_path(str(Path(__file__).with_name('import-teable-orders.py')))
    token = sys.stdin.readline().strip()
    if not token:
        parser.error('A token must be supplied on standard input, not as an argument.')
    api = helper['Api']('http://127.0.0.1:3000', token)
    state = json.loads(args.state.read_text(encoding='utf-8'))
    print(json.dumps(apply(api, state, data, lambda: helper['save'](args.state, state)), ensure_ascii=False))


if __name__ == '__main__':
    main()
