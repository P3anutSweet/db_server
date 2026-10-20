#!/usr/bin/env python3
"""Deploy the reviewed model and import a private seven-sheet order snapshot through Teable API.

Default is an offline plan. --apply requires TEABLE_TOKEN, an explicit space ID, and a private state file.
This imports historical rows as reviewable order-ledger drafts, never as confirmed pickup postings.
"""
import argparse
from collections import Counter
from decimal import Decimal
import datetime as dt
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request


EXTRA_FIELDS = {
    'markets': [('source_region', '原大区分组', 'text'), ('review_state', '归属核对', 'text')],
    'skus': [('review_state', '主数据核对', 'text')],
    'order_lines': [
        ('source_quantity', '台账数量', 'decimal'), ('source_picked', '台账已提货', 'decimal'),
        ('source_pending', '台账待提货', 'decimal'), ('source_batch', '原批次文字', 'text'),
        ('source_status', '原订单状态', 'text'), ('source_ean', '原EAN', 'text'),
        ('source_order_date', '源下单日期', 'date'), ('source_forecast_date', '源FCST提交日期', 'date'),
        ('source_planned_date', '源计划交付日期', 'date'), ('estimated_price', '原预估单价', 'decimal'),
        ('price_currency_state', '币种核对', 'text'), ('source_pickup_note', '原业务提货记录', 'longText'),
        ('source_delivery_note', '原客户交付记录', 'longText'), ('source_notes', '原备注及月度更新', 'longText'),
        ('source_owner', '原负责人文字', 'text'), ('source_spares', '原备品说明', 'longText'),
        ('source_sheet', '来源工作表', 'text'), ('source_row_number', '来源行号', 'integer'),
        ('review_state', '核对状态', 'text'), ('review_notes', '待核对内容', 'longText'),
    ],
}
VIEW_ORDER = ['__label', 'market', 'customer', 'sku', 'source_batch', 'source_quantity',
              'source_picked', 'source_pending', 'source_status', 'source_order_date',
              'source_forecast_date', 'source_planned_date', 'estimated_price', 'price_currency_state',
              'source_pickup_note', 'source_delivery_note', 'source_notes', 'review_state', 'review_notes']


class Api:
    def __init__(self, origin, token):
        if origin not in {'http://127.0.0.1:3000', 'http://localhost:3000'}:
            raise ValueError('Run on the server using its loopback API; no token transmission to other origins.')
        self.origin, self.token = origin, token

    def call(self, method, route, payload=None):
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        request = urllib.request.Request(self.origin + '/api' + route, data=data, method=method,
                    headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            # Do not log headers, payloads or arbitrary backend echoes of private records.
            raise RuntimeError(f'API {method} {route.split("?")[0]} failed: HTTP {error.code}') from None

    def records(self, table_id):
        result, skip = [], 0
        while True:
            page = self.call('GET', f'/table/{table_id}/record?fieldKeyType=id&take=1000&skip={skip}')['records']
            result.extend(page)
            if len(page) < 1000:
                return result
            skip += len(page)


def save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        path.parent.chmod(0o700)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if os.name != 'nt':
        temp.chmod(0o600)
    temp.replace(path)


def options(table, field, rows):
    kind = field['type']
    if kind in {'date', 'dateTime'}:
        return {'formatting': {'date': 'YYYY-MM-DD', 'time': 'None' if kind == 'date' else 'HH:mm', 'timeZone': 'Asia/Shanghai'}}
    if kind in {'decimal', 'integer'}:
        return {'formatting': {'type': 'decimal', 'precision': 0 if kind == 'integer' else 2}}
    if kind in {'singleSelect', 'multipleSelect'}:
        choices = []
        if table == 'skus' and field['key'] in {'brand', 'category'}:
            choices = sorted({str(r['values'].get(field['key'])) for r in rows if r['values'].get(field['key'])})
        elif field['key'] == 'applicable_to':
            choices = ['客户偏好', '产品标注']
        elif field['key'] == 'group' and table == 'tags':
            choices = ['产品定位', '灯效外观', '连接方式', '使用场景', '功能体验']
        elif table == 'review_issues' and field['key'] == 'kind':
            choices = ['导入核对', '条码', '料号', '批次', '品牌', '计划节点', '状态数量', '市场归属', '客户身份', '标签偏好']
        else:
            text = field['notes'].split('；')[0].split('。')[0]
            if '/' in text:
                choices = text.split('/')
        if table == 'line_plans' and field['key'] == 'state':
            choices = ['草稿', '有效', '已替代', '已取消']
        return {'choices': [{'name': c} for c in choices]}
    if kind == 'user':
        return {'isMultiple': False, 'shouldNotify': False}
    return {}


def field_ro(table, field, rows):
    types = {'text': 'singleLineText', 'longText': 'longText', 'integer': 'number', 'decimal': 'number',
             'date': 'date', 'dateTime': 'date', 'singleSelect': 'singleSelect',
             'multipleSelect': 'multipleSelect', 'checkbox': 'checkbox', 'attachment': 'attachment', 'user': 'user'}
    return {'name': field['name'], 'type': types[field['type']], 'options': options(table, field, rows),
            'description': field['notes']}


class Importer:
    def __init__(self, api, model, data, state_path, space_id):
        self.api, self.model, self.data, self.path = api, model, data, state_path
        self.state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {
            'source_sha256': data['sha256'], 'space_id': space_id, 'tables': {}, 'fields': {}, 'views': {}}
        if self.state['source_sha256'] != data['sha256'] or self.state['space_id'] != space_id:
            raise ValueError('State belongs to another source or space. Cross-version business reconciliation is required.')
        self.cache = {}

    def persist(self):
        save(self.path, self.state)

    def schema(self):
        state, api = self.state, self.api
        if state.get('operations_version'):
            raise ValueError('Database is maintained by refresh-order-operations.py; legacy import must not recreate removed modules.')
        if not state.get('base_id'):
            bases = api.call('GET', f'/space/{state["space_id"]}/base')
            if any(b['name'] == '外设海外订单' for b in bases):
                raise ValueError('Same-name base already exists without import state; refusing to adopt or overwrite it.')
            base = api.call('POST', '/base', {'spaceId': state['space_id'], 'name': '外设海外订单'})
            state['base_id'] = base['id']
            self.persist()
        existing = {t['name']: t for t in api.call('GET', f'/base/{state["base_id"]}/table')}
        deferred = []
        for table in self.model['tables']:
            key = table['key']
            name = '订单明细' if key == 'order_lines' else table['name']
            scalar = [f for f in table['fields'] if not f.get('target') and f['type'] != 'derived']
            scalar += [{'key': k, 'name': n, 'type': t, 'notes': '源台账快照/导入核对字段，不作为已确认履约凭据'}
                       for k, n, t in EXTRA_FIELDS.get(key, [])]
            deferred.extend(f'{key}.{f["key"]}' for f in table['fields'] if f['type'] == 'derived')
            if key not in state['tables']:
                if name in existing:
                    # Recovery from an uncertain create-table response is restricted to this dedicated base.
                    result = existing[name]
                else:
                    result = api.call('POST', f'/base/{state["base_id"]}/table/', {
                        'name': name, 'description': table['grain'], 'icon': None,
                        'fields': [{'name': '记录名称', 'type': 'singleLineText'},
                                   {'name': '导入键', 'type': 'singleLineText', 'unique': True}],
                        'views': [{'name': '全部订单台账' if key == 'order_lines' else '全部记录', 'type': 'grid'}],
                        'records': []})
                state['tables'][key] = result['id']
                self.persist()
            tid = state['tables'][key]
            fields = api.call('GET', f'/table/{tid}/field')
            by_name = {f['name']: f for f in fields}
            if '导入键' not in by_name or '记录名称' not in by_name:
                raise ValueError('Import-managed table markers absent; refusing unsafe recovery.')
            state['fields'][key] = {'__label': by_name['记录名称']['id'], '__key': by_name['导入键']['id']}
            for field in scalar:
                expected = field_ro(key, field, self.data['rows'])
                result = by_name.get(field['name'])
                if result is None:
                    result = api.call('POST', f'/table/{tid}/field', expected)
                if result['type'] != expected['type']:
                    raise ValueError('Existing field has unexpected type: ' + key + '.' + field['key'])
                state['fields'][key][field['key']] = result['id']
                self.persist()
            print('Prepared table: ' + key, flush=True)
        # Links require all target tables to exist. One-way links avoid duplicate reciprocal columns.
        for table in self.model['tables']:
            key, tid = table['key'], state['tables'][table['key']]
            existing_fields = {f['name']: f for f in api.call('GET', f'/table/{tid}/field')}
            for field in table['fields']:
                if not field.get('target'):
                    continue
                result = existing_fields.get(field['name'])
                foreign = state['tables'][field['target']]
                relationship = 'manyMany' if field['type'] == 'linkMany' else 'manyOne'
                if result is None:
                    result = api.call('POST', f'/table/{tid}/field', {'name': field['name'], 'type': 'link',
                        'description': field['notes'], 'options': {'relationship': relationship,
                        'foreignTableId': foreign, 'isOneWay': True}})
                if result['type'] != 'link' or result['options']['foreignTableId'] != foreign:
                    raise ValueError('Existing link targets a different table.')
                state['fields'][key][field['key']] = result['id']
                self.persist()
        state['deferred_computed_fields'] = deferred
        self.persist()

    def record(self, table, key, label, values):
        tid, fields = self.state['tables'][table], self.state['fields'][table]
        if table not in self.cache:
            self.cache[table] = {}
            for r in self.api.records(tid):
                oldkey = r['fields'].get(fields['__key'])
                if oldkey:
                    if oldkey in self.cache[table]:
                        raise ValueError('Duplicate managed import key; reconciliation needed.')
                    self.cache[table][oldkey] = r
        if key in self.cache[table]:
            return self.cache[table][key]['id']  # Do not overwrite later user edits.
        body = {fields[k]: v for k, v in values.items() if v is not None}
        body.update({fields['__label']: str(label), fields['__key']: key})
        result = self.api.call('POST', f'/table/{tid}/record', {'fieldKeyType': 'id', 'typecast': False,
                                  'records': [{'fields': body}]})['records'][0]
        self.cache[table][key] = result
        return result['id']

    @staticmethod
    def one(rid):
        return {'id': rid}

    def run(self):
        self.schema()
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        regions = {r['code']: self.record('regions', 'region:' + r['code'], r['name'],
                   {'code': r['code'], 'name': r['name'], 'active': True}) for r in self.model['region_seeds']}
        region_map = {'巴西': 'SA', '阿根廷': 'SA', '波兰': 'EU', '土耳其': 'EU', '越南': 'SEA', '菲律宾': 'SEA'}
        markets = {}
        for sheet in self.data['sheets']:
            name, region = sheet['market'], region_map.get(sheet['market'])
            markets[name] = self.record('markets', 'market:' + name, name, {'code': name, 'name': name,
                'region': self.one(regions[region]) if region else None, 'active': True,
                'source_region': sheet['sheet'], 'review_state': '按原业务分组' if region else '大区归属待核对'})
        for tag in self.model['tag_examples']:
            self.record('tags', 'tag:' + tag['code'], tag['name'], {**tag, 'active': True})
        snapshot = self.record('import_batches', 'snapshot:' + self.data['sha256'], self.data['filename'],
            {'filename': self.data['filename'], 'sha256': self.data['sha256'],
             'mapping_version': self.data['mapping_version'], 'imported_at': now})
        skus, source_ids, line_ids = {}, {}, {}
        for row in self.data['rows']:
            values, sku = row['values'], None
            if row['part']:
                sku_key = str(values.get('brand') or '') + ':' + row['part']
                if sku_key not in skus:
                    skus[sku_key] = self.record('skus', 'order-source-sku:' + sku_key, row['part'],
                        {'part_number': row['part'], 'brand': values.get('brand'), 'category': values.get('category'),
                         'description_zh': values.get('model'), 'review_state': '来自订单，待产品主数据核对'})
                sku = skus[sku_key]
            src = self.record('source_rows', row['source_key'], f'{row["sheet"]}!{row["row"]}',
                {'snapshot': self.one(snapshot), 'sheet': row['sheet'], 'row': row['row'],
                 'business_values': json.dumps({'values': values, 'cells': row['cells']}, ensure_ascii=False),
                 'classification': '待确认'})
            notes = {k[5:]: v for k, v in values.items() if k.startswith('note:') and v is not None}
            line = self.record('order_lines', 'ledger:' + row['source_key'],
                f'{row["market"]} {row["part"] or values.get("model", "")} 源行{row["row"]}',
                {'sku': self.one(sku) if sku else None, 'market': self.one(markets[row['market']]),
                 'description_snapshot': values.get('model'), 'requirements': values.get('requirements'),
                 'source_rows': [self.one(src)], 'source_batch': str(values['batch']) if values.get('batch') else None,
                 'source_quantity': row['quantities']['quantity'], 'source_picked': row['quantities']['picked'],
                 'source_pending': row['quantities']['pending'], 'source_status': values.get('source_status'),
                 'source_ean': str(values['ean']) if values.get('ean') is not None else None,
                 'source_order_date': self.date(row['dates']['order_date']),
                 'source_forecast_date': self.date(row['dates']['forecast_date']),
                 'source_planned_date': self.date(row['dates']['planned_date']),
                 'estimated_price': values.get('estimated_price') if isinstance(values.get('estimated_price'), (int, float)) else None,
                 'price_currency_state': '待确认' if values.get('estimated_price') is not None else None,
                 'source_pickup_note': self.text(values.get('pickup_note')),
                 'source_delivery_note': self.text(values.get('delivery_note')),
                 'source_notes': json.dumps(notes, ensure_ascii=False) if notes else None,
                 'source_owner': self.text(values.get('owner_raw')), 'source_spares': self.text(values.get('spares_raw')),
                 'source_sheet': row['sheet'], 'source_row_number': row['row'],
                 'review_state': '待核对', 'review_notes': '\n'.join(row['issues'])})
            source_ids[(row['sheet'], row['row'])] = src
            line_ids[(row['sheet'], row['row'])] = line
            # Safely fill only the import-owned source-row backlink; never overwrite order edits.
            backfield = self.state['fields']['source_rows']['matched_line']
            backlink = self.cache['source_rows'][row['source_key']]['fields'].get(backfield)
            linked = backlink[0]['id'] if isinstance(backlink, list) and backlink else backlink.get('id') if isinstance(backlink, dict) else None
            if linked != line:
                self.api.call('PATCH', f'/table/{self.state["tables"]["source_rows"]}/record/{src}', {
                    'fieldKeyType': 'id', 'typecast': False, 'record': {'fields': {backfield: self.one(line)}}})
            planned = row['dates']['planned_date']
            if planned:
                self.record('line_plans', 'source-plan:' + row['source_key'],
                    f'{row["market"]} 源行{row["row"]} {planned}',
                    {'line': self.one(line), 'plan_kind': '原计划节点待核对', 'month': planned[:7],
                     'exact_date': self.date(planned), 'planned_qty': row['quantities']['quantity'],
                     'state': '草稿', 'change_reason': '来自原计划交付列，节点/粒度待核对，不计正式有效计划'})
            self.record('review_issues', 'review:' + row['source_key'], f'{row["sheet"]}!{row["row"]} 导入核对',
                {'source': self.one(src), 'kind': '导入核对', 'state': '未处理', 'decision': '\n'.join(row['issues'])})
        # Preserve shared merged-cell notes once and link all covered lines; no blanket forward fill.
        for note in self.data['shared_notes']:
            links = [self.one(line_ids[(note['sheet'], row)]) for row in note['rows']]
            self.record('followups', 'merged:' + self.data['sha256'] + ':' + note['sheet'] + ':' + note['range'],
                note['sheet'] + ' ' + note['range'], {'lines': links,
                    'body': note['header'] + '\n' + note['text'],
                    'source_row': self.one(source_ids[(note['sheet'], note['rows'][0])])})
        self.views(markets)
        report = self.verify()
        self.state['report'] = report
        self.state['completed_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        self.persist()
        print(json.dumps(report, ensure_ascii=False), flush=True)

    @staticmethod
    def date(value):
        return value + 'T00:00:00.000Z' if value else None

    @staticmethod
    def text(value):
        return str(value) if value is not None else None

    def views(self, markets):
        tid, f = self.state['tables']['order_lines'], self.state['fields']['order_lines']
        existing = {v['name']: v for v in self.api.call('GET', f'/table/{tid}/view')}
        visible = [k for k in VIEW_ORDER if k in f]
        column_meta = {fid: {'order': visible.index(k) if k in visible else 100 + i,
                                  'hidden': k not in visible, 'width': 160}
                       for i, (k, fid) in enumerate(f.items())}
        default = existing.get('全部订单台账')
        if default:
            self.api.call('PUT', f'/table/{tid}/view/{default["id"]}/column-meta',
                [{'fieldId': fid, 'columnMeta': value} for fid, value in column_meta.items()])
            self.state['views']['all_orders'] = default['id']
        for market in markets:
            name = market + '订单'
            if name not in existing:
                result = self.api.call('POST', f'/table/{tid}/view', {'name': name, 'type': 'grid',
                    'columnMeta': column_meta, 'filter': {'conjunction': 'and', 'filterSet': [
                        {'fieldId': f['source_sheet'], 'operator': 'is', 'value':
                         next(s['sheet'] for s in self.data['sheets'] if s['market'] == market)}]}})
                self.state['views'][market] = result['id']
            else:
                self.state['views'][market] = existing[name]['id']
        self.persist()

    def verify(self):
        tid, fields = self.state['tables']['order_lines'], self.state['fields']['order_lines']
        records = self.api.records(tid)
        expected_keys = {'ledger:' + r['source_key'] for r in self.data['rows']}
        managed = [r for r in records if r['fields'].get(fields['__key']) in expected_keys]
        if len(managed) != len(expected_keys):
            raise ValueError('Read-back record count does not match source or duplicate source keys exist.')
        counts, totals = Counter(), {k: Decimal(0) for k in ('quantity', 'picked', 'pending')}
        bykey = {r['fields'][fields['__key']]: r['fields'] for r in managed}
        checks = {'source_quantity': 'quantity', 'source_picked': 'picked', 'source_pending': 'pending'}
        for row in self.data['rows']:
            found = bykey['ledger:' + row['source_key']]
            counts[row['market']] += 1
            for field, key in checks.items():
                actual, expected = found.get(fields[field]), row['quantities'][key]
                if actual != expected:
                    raise ValueError('Read-back row quantity mismatch; no automatic overwrite attempted.')
                if actual is not None:
                    totals[key] += Decimal(str(actual))
            links = found.get(fields['market'])
            if not links or (links[0]['id'] if isinstance(links, list) else links['id']) != self.cache['markets']['market:' + row['market']]['id']:
                raise ValueError('Market link mismatch.')
            for field, expected in {
                'source_sheet': row['sheet'], 'source_row_number': row['row'],
                'source_status': row['values'].get('source_status'),
                'source_pickup_note': self.text(row['values'].get('pickup_note')),
                'source_delivery_note': self.text(row['values'].get('delivery_note')),
                'estimated_price': row['values'].get('estimated_price'),
            }.items():
                if found.get(fields[field]) != expected:
                    raise ValueError('Read-back source field mismatch: ' + field)
            for field, date_key in [('source_order_date', 'order_date'), ('source_forecast_date', 'forecast_date'), ('source_planned_date', 'planned_date')]:
                actual = found.get(fields[field])
                if (actual[:10] if actual else None) != row['dates'][date_key]:
                    raise ValueError('Read-back source date mismatch.')
        if {k: str(v) for k, v in totals.items()} != self.data['summary']['totals']:
            raise ValueError('Read-back totals do not reconcile to source.')
        view_counts = {}
        for market, expected in self.data['summary']['by_market'].items():
            view_id = self.state['views'][market]
            view_rows = self.api.call('GET', f'/table/{tid}/record?viewId={view_id}&fieldKeyType=id&take=1000')['records']
            if len(view_rows) != expected:
                raise ValueError('Market view filter count mismatch.')
            view_counts[market] = len(view_rows)
        return {'base_id': self.state['base_id'], 'table_id': tid, 'rows': len(managed),
                'by_market': dict(counts), 'totals': {k: str(v) for k, v in totals.items()},
                'verified_view_counts': view_counts,
                'url': 'https://192.168.32.84/base/' + self.state['base_id'] + '/' + tid,
                'status': 'historical_ledger_imported_pending_business_review',
                'deferred_computed_fields': self.state['deferred_computed_fields']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True, type=Path)
    parser.add_argument('--data', required=True, type=Path)
    parser.add_argument('--state', type=Path)
    parser.add_argument('--space-id')
    parser.add_argument('--origin', default='http://127.0.0.1:3000')
    parser.add_argument('--token-stdin', action='store_true', help='Read token from standard input, never command arguments')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    model = json.loads(args.model.read_text(encoding='utf-8'))
    data = json.loads(args.data.read_text(encoding='utf-8'))
    if len({r['source_key'] for r in data['rows']}) != len(data['rows']):
        raise ValueError('Duplicate source keys in payload')
    if not args.apply:
        print(json.dumps({'action': 'offline_plan_only', 'tables': len(model['tables']), **data['summary']}, ensure_ascii=False))
        return
    token = sys.stdin.readline().strip() if args.token_stdin else os.environ.get('TEABLE_TOKEN', '').strip()
    if not args.state or not args.space_id or not token:
        parser.error('--apply requires --state, --space-id and a token via --token-stdin or TEABLE_TOKEN')
    Importer(Api(args.origin, token), model, data, args.state, args.space_id).run()


if __name__ == '__main__':
    main()
