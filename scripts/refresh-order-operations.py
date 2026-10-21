#!/usr/bin/env python3
"""Migrate and refresh the simplified Teable order database via its local API.

Run --setup once; subsequent runs only refresh identifiers and monthly aggregates.
Credentials, counter reservations and source rows stay outside the repository.
"""
import argparse
from collections import Counter, defaultdict
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import runpy
import sys
from zoneinfo import ZoneInfo

RULES_PATH = Path(__file__).resolve().parents[1] / 'tools/schema/order_numbers.py'
if not RULES_PATH.exists():
    RULES_PATH = Path(__file__).with_name('order_numbers.py')
rules = runpy.run_path(str(RULES_PATH))
COUNTRIES, CHANNEL_STATES, ADMIN_STATES = (rules[k] for k in ('COUNTRIES', 'CHANNEL_STATES', 'ADMIN_STATES'))
MINIMAL_TABLES = {'markets', 'customers', 'tags', 'skus', 'order_batches', 'order_lines', 'monthly_overview'}
LAYOUT_VERSION = '3.1'


def linked(value):
    if not value:
        return []
    return [v['id'] for v in (value if isinstance(value, list) else [value])]


def comparable(value):
    """Compare link identities without server-added display titles."""
    if isinstance(value, dict) and 'id' in value:
        return {'id': value['id']}
    if isinstance(value, list):
        return [comparable(v) for v in value]
    return value


class Operations:
    def __init__(self, folder, token_file):
        self.folder = folder
        helper = runpy.run_path(str(folder / 'import-teable-orders.py'))
        self.save = helper['save']
        self.api = helper['Api']('http://127.0.0.1:3000', token_file.read_text().strip())
        self.state = json.loads((folder / 'state.json').read_text())
        self.tables, self.fields = self.state['tables'], self.state['fields']
        self.catalog = self.api.call('GET', f'/base/{self.state["base_id"]}/table')
        self.active = {t['id'] for t in self.catalog}
        self.source = None if self.state.get('structure_version') == 'minimal-7-v1' else json.loads((folder / 'orders-import-private.json').read_text())
        self.counter = rules['CountryCounter'](folder / 'order-counters.sqlite3')

    def persist(self):
        self.save(self.folder / 'state.json', self.state)

    def rows(self, table):
        return self.api.records(self.tables[table])

    def get(self, row, table, key):
        return row['fields'].get(self.fields[table].get(key))

    def patch(self, table, row, values):
        updates = {self.fields[table][k]: v for k, v in values.items()
                   if comparable(row['fields'].get(self.fields[table][k])) != comparable(v)}
        if not updates:
            return
        self.api.call('PATCH', f'/table/{self.tables[table]}/record/{row["id"]}',
                      {'fieldKeyType': 'id', 'typecast': False, 'record': {'fields': updates}})
        row['fields'].update(updates)

    def insert(self, table, values):
        return self.api.call('POST', f'/table/{self.tables[table]}/record',
            {'fieldKeyType': 'id', 'typecast': False,
             'records': [{'fields': {self.fields[table][k]: v for k, v in values.items()}}]})['records'][0]

    def field(self, table, key, name, kind='singleLineText', options=None, **extra):
        tid = self.tables[table]
        fields = self.api.call('GET', f'/table/{tid}/field')
        existing = next((f for f in fields if f['id'] == self.fields.get(table, {}).get(key)), None)
        existing = existing or next((f for f in fields if f['name'] == name), None)
        ro = {'name': name, 'type': kind, 'options': options or {}, **extra}
        if existing is None:
            existing = self.api.call('POST', f'/table/{tid}/field', ro)
        if existing['type'] != kind or bool(existing.get('isLookup')) != bool(extra.get('isLookup')):
            raise ValueError('Unexpected existing field type: ' + table + '.' + key)
        if existing['name'] != name:
            self.api.call('PATCH', f'/table/{tid}/field/{existing["id"]}', {'name': name})
        self.fields.setdefault(table, {})[key] = existing['id']
        self.persist()
        return existing['id']

    def number(self, table, key, name, precision=0, **extra):
        return self.field(table, key, name, 'number', {'formatting': {'type': 'decimal', 'precision': precision}}, **extra)

    def formula(self, table, key, name, expression, number=True):
        opts = {'expression': expression, 'timeZone': 'Asia/Shanghai'}
        if number:
            opts['formatting'] = {'type': 'decimal', 'precision': 2}
        return self.field(table, key, name, 'formula', opts)

    def link(self, table, key, name, foreign, multiple=False):
        return self.field(table, key, name, 'link', {'relationship': 'manyMany' if multiple else 'manyOne',
            'foreignTableId': self.tables[foreign], 'isOneWay': True})

    def lookup(self, table, key, name, link, foreign, source, kind='singleLineText'):
        return self.field(table, key, name, kind, isLookup=True, lookupOptions={
            'linkFieldId': self.fields[table][link], 'foreignTableId': self.tables[foreign],
            'lookupFieldId': self.fields[foreign][source]})

    def ref(self, table, key):
        return '{' + self.fields[table][key] + '}'

    def setup(self):
        if self.state.get('structure_version') == 'minimal-7-v1':
            self.validate_minimal()
            self.layouts()
            self.state['operations_layout_version'] = LAYOUT_VERSION
            self.persist()
            return
        if self.state.get('minimal_migration'):
            raise ValueError('Resume migrate-minimal-structure.py; do not rerun the historical setup during consolidation.')
        # Archive metadata and every active record before making schema changes.
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        self.save(self.folder / ('operations-backup-' + stamp + '-private.json'), {
            'state': self.state, 'tables': [{'table': t, 'records': self.api.records(t['id']),
                'fields': self.api.call('GET', f'/table/{t["id"]}/field'),
                'views': self.api.call('GET', f'/table/{t["id"]}/view')} for t in self.catalog]})
        for key in list(self.tables):
            if self.tables[key] not in self.active:
                self.state.setdefault('removed_tables', {})[key] = self.tables.pop(key)
                self.state.setdefault('removed_fields', {})[key] = self.fields.pop(key)
        self.persist()
        self.field('order_lines', 'order_date', '下单日期', 'date',
            {'formatting': {'date': 'YYYY-MM-DD', 'time': 'None', 'timeZone': 'Asia/Shanghai'}})
        self.number('order_lines', 'order_quantity', '实际订单数量')
        self.number('order_lines', 'picked_quantity', '已提货数量')
        self.number('order_lines', 'unit_price', '预估单价 USD', 2)
        self.field('order_lines', 'channel_status', '渠道业务状态', 'singleSelect',
            {'choices': [{'name': n} for n in CHANNEL_STATES]})
        self.field('order_lines', 'admin_status', '管理状态', 'singleSelect',
            {'choices': [{'name': n} for n in ADMIN_STATES]})
        for key, name in [('number_prefix', '编号前缀快照'), ('country_code', '国家编码'),
                          ('region_code', '大区编码'), ('order_month', '下单月份'),
                          ('automation_note', '处理提示'), ('migration_marker', '业务字段初始化版本')]:
            self.field('order_lines', key, name)
        self.number('order_lines', 'country_sequence', '国家连续序号')
        self.field('order_lines', 'followup_text', '跟进记录', 'longText')
        self.field('order_lines', 'followup_ids', '已合并跟进标识', 'longText')
        r = lambda k: self.ref('order_lines', k)
        self.formula('order_lines', 'order_number', '订单号',
            f'IF(COUNTA({r("country_sequence")})=0,BLANK(),CONCATENATE({r("number_prefix")},RIGHT(CONCATENATE("000000",{r("country_sequence")}),6)))', False)
        self.formula('order_lines', 'pending_quantity', '未提货数量',
            f'IF(OR(COUNTA({r("order_quantity")})=0,COUNTA({r("picked_quantity")})=0),BLANK(),{r("order_quantity")}-{r("picked_quantity")})')
        self.formula('order_lines', 'order_value', '订单总价 USD',
            f'IF(OR(COUNTA({r("order_quantity")})=0,COUNTA({r("unit_price")})=0),BLANK(),{r("order_quantity")}*{r("unit_price")})')
        self.number('markets', 'sequence_highwater', '订单序号保留上限')
        regions = {self.get(row, 'regions', 'code'): row['id'] for row in self.rows('regions')}
        for market in self.rows('markets'):
            name = self.get(market, 'markets', 'name')
            if name in COUNTRIES:
                region, code = COUNTRIES[name]
                self.patch('markets', market, {'code': code, 'region': {'id': regions[region]},
                    'review_state': '用户业务分组已确认'})
        self.field('order_batches', 'batch_number', '订单批次名称')
        self.field('order_batches', 'channel', '原渠道文字（归档）')

        # This table remains a source delivery-plan reference; never count its 94
        # ambiguous source rows as the user's monthly business forecasts.
        self.field('line_plans', 'forecast_state', '原FCST提交状态（参考）', 'singleSelect')
        self.field('line_plans', 'submitted_date', '原FCST提交日期（参考）', 'date')
        kind = next(f for f in self.api.call('GET', f'/table/{self.tables["line_plans"]}/field')
                    if f['id'] == self.fields['line_plans']['plan_kind'])
        if any(c['name'] == '基地预测' for c in kind['options']['choices']):
            if any(self.get(row, 'line_plans', 'plan_kind') == '基地预测' for row in self.rows('line_plans')):
                raise ValueError('Existing base forecasts require review before reclassification.')
            opts = dict(kind['options'])
            opts['choices'] = [dict(c, name='业务预测') if c['name'] == '基地预测' else c for c in opts['choices']]
            self.api.call('PUT', f'/table/{self.tables["line_plans"]}/field/{kind["id"]}/convert',
                          {'name': kind['name'], 'type': kind['type'], 'options': opts})
        self.merge_followups()
        self.seed_orders()
        self.summary_schema()
        self.seed_forecasts()
        self.state['operations_version'] = '3.0'
        self.state.pop('operations_layout_version', None)
        self.persist()

    def merge_followups(self):
        archive_path = self.folder / 'followups-archive-private.json'
        restored = False
        old_id = self.tables.get('followups') or self.state.get('removed_tables', {}).get('followups')
        old_fields = self.fields.get('followups') or self.state.get('removed_fields', {}).get('followups')
        if archive_path.exists():
            archive = json.loads(archive_path.read_text())
        elif old_id:
            # The user already removed this table. Temporarily restore it solely to
            # read and archive all current text, then trash it again after verification.
            if old_id not in self.active:
                trash = self.api.call('GET', f'/trash/items?resourceId={self.state["base_id"]}&resourceType=base')
                item = next((t for t in trash['trashItems'] if t.get('resourceId') == old_id), None)
                if item is None:
                    raise ValueError('Follow-up archive unavailable; do not discard history.')
                self.api.call('POST', '/trash/restore/' + item['id'])
                restored = True
            archive = {'fields': old_fields, 'records': self.api.records(old_id)}
            self.save(archive_path, archive)
        else:
            archive = {'fields': {}, 'records': []}
        orders = self.rows('order_lines')
        byid = {r['id']: r for r in orders}
        bysource = {(self.get(r, 'order_lines', 'source_sheet'), self.get(r, 'order_lines', 'source_row_number')): r for r in orders}
        additions = defaultdict(list)
        for row in archive['records']:
            f = archive['fields']
            text = row['fields'].get(f['body'])
            value = row['fields'].get(f['lines'])
            if isinstance(value, str):
                # Teable converts links to labels when a deleted table is restored.
                # Recover imported relations from the immutable source key, never
                # by splitting titles or matching potentially duplicate product names.
                source_key = row['fields'].get(f['__key'])
                source_note = next((n for n in self.source['shared_notes'] if source_key ==
                    'merged:' + self.source['sha256'] + ':' + n['sheet'] + ':' + n['range']), None)
                targets = [bysource[(source_note['sheet'], n)]['id'] for n in source_note['rows']
                           if (source_note['sheet'], n) in bysource] if source_note else []
            else:
                targets = linked(value)
            if text and not targets:
                raise ValueError('Unlinked follow-up retained in archive; manual assignment needed.')
            for rid in targets:
                if rid not in byid:
                    raise ValueError('Follow-up references a missing order; do not discard it.')
                additions[rid].append(('followup:' + row['id'], text))
        # Source merged notes remain recoverable even if table restore filtered links.
        for note in self.source['shared_notes']:
            token = 'source-note:' + note['sheet'] + ':' + note['range']
            text = note['header'] + '\n' + note['text']
            for n in note['rows']:
                row = bysource.get((note['sheet'], n))
                if row and not any(t[1] == text for t in additions[row['id']]):
                    additions[row['id']].append((token, text))
        for row in orders:
            ids = set((self.get(row, 'order_lines', 'followup_ids') or '').splitlines())
            text = self.get(row, 'order_lines', 'followup_text') or ''
            for token, body in additions[row['id']]:
                if token not in ids:
                    text += ('\n\n' if text else '') + body
                    ids.add(token)
            self.patch('order_lines', row, {'followup_text': text or None, 'followup_ids': '\n'.join(sorted(ids)) or None})
        current = {r['id']: r for r in self.rows('order_lines')}
        for rid, items in additions.items():
            text = self.get(current[rid], 'order_lines', 'followup_text') or ''
            if any(body not in text for _, body in items):
                raise ValueError('Follow-up merge failed read-back; original retained.')
        if old_id and (restored or old_id in self.active):
            self.api.call('DELETE', f'/base/{self.state["base_id"]}/table/{old_id}')
            if 'followups' in self.tables:
                self.state.setdefault('removed_tables', {})['followups'] = self.tables.pop('followups')
                self.state.setdefault('removed_fields', {})['followups'] = self.fields.pop('followups')
        self.state['merged_followup_count'] = len(archive['records'])
        self.persist()

    def seed_orders(self):
        # Seed once per existing row, never overwrite future user edits on refresh.
        for row in self.rows('order_lines'):
            if self.get(row, 'order_lines', 'migration_marker'):
                continue
            get = lambda k: self.get(row, 'order_lines', k)
            date, source_status = get('source_order_date'), get('source_status')
            status = {'业务提货中': '提货中', '客户付款中': '客户付款中', '业务交付中': '已提货'}.get(source_status)
            q, picked = get('source_quantity'), get('source_picked')
            # A source label alone cannot prove complete pickup or delivery.
            if source_status == '业务交付中' and (q is None or picked != q):
                status = None
            if status is None and date:
                status = '已下单'
            values = {'migration_marker': '3.0', 'order_date': date, 'order_quantity': q,
                      'picked_quantity': picked, 'unit_price': get('estimated_price'),
                      'channel_status': status, 'admin_status': '需确认订单状态'}
            if get('channel_status'):
                values.pop('channel_status')
            if get('admin_status'):
                values.pop('admin_status')
            self.patch('order_lines', row, values)

    def summary_schema(self):
        key = 'monthly_overview'
        if key not in self.tables:
            found = next((t for t in self.catalog if t['name'] == '月度总览'), None)
            if found is None:
                found = self.api.call('POST', f'/base/{self.state["base_id"]}/table/', {
                    'name': '月度总览', 'description': '按月份、地区市场、SKU对比业务预测与实际订单；美元估值。',
                    'fields': [{'name': '记录名称', 'type': 'singleLineText'},
                               {'name': '汇总键', 'type': 'singleLineText', 'unique': True}],
                    'views': [{'name': '当月概览', 'type': 'grid'}], 'records': []})
            self.tables[key] = found['id']
            fs = self.api.call('GET', f'/table/{found["id"]}/field')
            self.fields[key] = {'__label': next(f['id'] for f in fs if f['name'] == '记录名称'),
                                '__key': next(f['id'] for f in fs if f['name'] == '汇总键')}
            self.persist()
        self.field(key, 'month', '月份')
        self.link(key, 'market', '地区市场', 'markets')
        self.lookup(key, 'business_owner', '业务负责人', 'market', 'markets', 'business_owner')
        self.link(key, 'sku', '产品SKU', 'skus')
        self.lookup(key, 'product_name', '产品说明', 'sku', 'skus', 'description_zh', 'longText')
        self.number(key, 'forecast_quantity', '业务预测数量')
        self.number(key, 'forecast_price', '预估单价 USD', 2)
        for k, n in [('known_quantity', '已登记订单数量'), ('known_picked', '已登记已提货数量'),
                     ('known_pending', '已登记未提货数量'), ('date_gap', '市场待补日期订单数'),
                     ('invalid_count', '数量或状态待确认订单数'), ('missing_price', '缺价订单数')]:
            self.number(key, k, n)
        self.number(key, 'known_value', '已登记订单估值 USD', 2)
        self.field(key, 'coverage_note', '统计说明', 'longText')
        self.field(key, 'forecast_source', '业务预测来源', 'longText')
        self.field(key, 'forecast_initialized', '预测初始化版本')
        self.field(key, 'refreshed_at', '刷新时间')
        self.link(key, 'orders', '关联订单', 'order_lines', True)
        r = lambda k: self.ref(key, k)
        guard = f'OR({r("date_gap")}>0,{r("invalid_count")}>0)'
        for logical, name, source in [('actual_quantity', '实际订单数量', 'known_quantity'),
                                    ('picked_quantity', '已提货数量', 'known_picked'),
                                    ('pending_quantity', '未提货数量', 'known_pending')]:
            self.formula(key, logical, name, f'IF({guard},BLANK(),{r(source)})')
        self.formula(key, 'forecast_value', '预测估值 USD',
            f'IF(OR(COUNTA({r("forecast_quantity")})=0,COUNTA({r("forecast_price")})=0),BLANK(),{r("forecast_quantity")}*{r("forecast_price")})')
        self.formula(key, 'actual_value', '订单总价 USD',
            f'IF(OR({guard},{r("missing_price")}>0),BLANK(),{r("known_value")})')

    def seed_forecasts(self):
        orders = {self.get(r, 'order_lines', '__key'): r for r in self.rows('order_lines')}
        totals = defaultdict(list)
        for row in self.source['rows']:
            order = orders.get('ledger:' + row['source_key'])
            if not order:
                raise ValueError('Source order missing; no forecast attached to a guessed product.')
            market = linked(self.get(order, 'order_lines', 'market'))
            sku = linked(self.get(order, 'order_lines', 'sku'))
            for month in (10, 11):
                q = row['values'].get(f'note:{month}月')
                if q is None or isinstance(q, bool):
                    continue
                if not isinstance(q, (int, float)):
                    raise ValueError('Non-numeric forecast plan needs explicit review.')
                if len(market) != 1 or len(sku) != 1:
                    raise ValueError('Forecast market/product identity is ambiguous.')
                key = (f'2026-{month:02}', market[0], sku[0])
                totals[key].append((q, row['values'].get('estimated_price'), row['sheet'], row['row']))
        existing = {self.get(r, 'monthly_overview', '__key'): r for r in self.rows('monthly_overview')}
        for key, sources in totals.items():
            stable = '|'.join(key)
            row = existing.get(stable)
            if row and self.get(row, 'monthly_overview', 'forecast_initialized'):
                continue
            prices = {x[1] for x in sources}
            price = next(iter(prices)) if len(prices) == 1 else None
            values = {'month': key[0], 'market': {'id': key[1]}, 'sku': {'id': key[2]},
                      'forecast_quantity': sum(x[0] for x in sources), 'forecast_price': price,
                      'forecast_initialized': 'excel-2026-months-10-11',
                      'forecast_source': '渠道业务预测；2026年10月/11月计划列；' + self.source['sha256'] + '\n' +
                          '\n'.join(f'{x[2]} 行{x[3]}' for x in sources)}
            if row:
                self.patch('monthly_overview', row, values)
            else:
                self.insert('monthly_overview', {'__key': stable, '__label': key[0], **values})

    def refresh(self):
        if self.state.get('operations_version') != '3.0':
            raise ValueError('Complete --setup before enabling refresh.')
        if self.state.get('minimal_migration') and self.state.get('structure_version') != 'minimal-7-v1':
            raise ValueError('Structure migration pending; resume migrate-minimal-structure.py.')
        if self.state.get('structure_version') == 'minimal-7-v1':
            self.validate_minimal()
        markets = {r['id']: r for r in self.rows('markets')}
        names = {rid: self.get(row, 'markets', 'name') for rid, row in markets.items()}
        for rid, row in markets.items():
            if names[rid] in COUNTRIES:
                self.counter.floor(COUNTRIES[names[rid]][1], int(self.get(row, 'markets', 'sequence_highwater') or 0))
        orders = self.rows('order_lines')
        seen, highs = set(), defaultdict(int)
        # Import all existing reservations first, independent of source row order.
        for row in orders:
            seq = self.get(row, 'order_lines', 'country_sequence')
            code = self.get(row, 'order_lines', 'country_code')
            if seq is not None and code:
                self.counter.reserve(code, row['id'], int(seq))
                highs[code] = max(highs[code], int(seq))
        normalized = []
        for row in sorted(orders, key=lambda r: (self.get(r, 'order_lines', 'source_sheet') or '',
                    self.get(r, 'order_lines', 'order_date') or '9999',
                    self.get(r, 'order_lines', 'source_row_number') or 10**9, r['id'])):
            get = lambda k: self.get(row, 'order_lines', k)
            market = linked(get('market')); sku = linked(get('sku'))
            note, updates = [], {}
            day = rules['business_day'](get('order_date'))
            name = names.get(market[0]) if len(market) == 1 else None
            if name not in COUNTRIES:
                note.append('需确认地区市场及国家编码')
            elif not day:
                note.append('待补真实下单日期；尚未生成订单号')
                if not get('country_sequence'):
                    updates.update(region_code=COUNTRIES[name][0], country_code=COUNTRIES[name][1])
            else:
                region, code = COUNTRIES[name]
                prefix = f'{region}{day:%Y%m%d}{code}'
                previous = get('number_prefix')
                seq = get('country_sequence')
                if get('channel_status') == '预测' and not previous:
                    note.append('业务预测阶段；实际下单后再生成订单号')
                elif previous and previous != prefix:
                    note.append('下单日期或国家与已生成订单号不同，请管理员确认；保留原号')
                else:
                    seq = self.counter.reserve(code, row['id'], int(seq) if seq is not None else None)
                    number = rules['order_number'](region, code, day, seq)
                    if number in seen:
                        raise ValueError('Duplicate managed order number; refresh stopped.')
                    seen.add(number)
                    updates.update(number_prefix=prefix, country_sequence=seq, country_code=code, region_code=region)
                    highs[code] = max(highs[code], seq)
            q, picked = get('order_quantity'), get('picked_quantity')
            if q is None or picked is None or q < 0 or picked < 0 or picked > q:
                note.append('需确认订单数量与已提货数量')
            if get('channel_status') not in CHANNEL_STATES:
                note.append('需确认渠道业务状态；原状态保留在历史字段')
            if not get('admin_status'):
                updates['admin_status'] = '需确认订单状态'
            if len(sku) != 1:
                note.append('需确认产品SKU')
            updates.update(order_month=day.strftime('%Y-%m') if day else None,
                           automation_note='；'.join(note) or None)
            self.patch('order_lines', row, updates)
            normalized.append({'id': row['id'], 'market': market[0] if len(market) == 1 else None,
                'sku': sku[0] if len(sku) == 1 else None, 'order_date': get('order_date'),
                'channel_status': get('channel_status'), 'order_quantity': q, 'picked_quantity': picked,
                'unit_price': get('unit_price')})
        for rid, row in markets.items():
            if names[rid] in COUNTRIES:
                code = COUNTRIES[names[rid]][1]
                # Include reservations made before a failed API update in the backed-up high-water mark.
                with self.counter.connect() as db:
                    result = db.execute('SELECT value FROM counters WHERE country=?', (code,)).fetchone()
                self.patch('markets', row, {'sequence_highwater': max(highs[code], result[0] if result else 0)})
        self.refresh_batches(orders, names)
        self.refresh_months(normalized, names)
        month = dt.datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m')
        if self.state.get('operations_layout_version') != LAYOUT_VERSION:
            self.layouts()
            self.state['operations_layout_version'] = LAYOUT_VERSION
            self.state['current_month_filter'] = month
            self.persist()
        elif self.state.get('current_month_filter') != month:
            view = self.state['operations_views']['当月概览']
            self.api.call('PUT', f'/table/{self.tables["monthly_overview"]}/view/{view}/filter', {
                'filter': {'conjunction': 'and', 'filterSet': [
                    {'fieldId': self.fields['monthly_overview']['month'], 'operator': 'is', 'value': month}]}})
            self.state['current_month_filter'] = month
            self.persist()
        report = {'at': dt.datetime.now(dt.timezone.utc).isoformat(), 'orders': len(orders),
                  'numbered': sum(bool(self.get(r, 'order_lines', 'country_sequence')) for r in orders),
                  'awaiting_dates': sum(not self.get(r, 'order_lines', 'order_date') for r in orders),
                  'monthly_rows': len(self.rows('monthly_overview'))}
        self.save(self.folder / 'operations-last-success.json', report)
        print(json.dumps(report, ensure_ascii=False))

    def refresh_batches(self, orders, names):
        current = {self.get(r, 'order_batches', '__key'): r for r in self.rows('order_batches')}
        for row in orders:
            raw = self.get(row, 'order_lines', 'source_batch')
            if not raw or linked(self.get(row, 'order_lines', 'batch')):
                continue
            market = linked(self.get(row, 'order_lines', 'market'))
            if len(market) != 1 or names.get(market[0]) not in COUNTRIES:
                continue
            code = COUNTRIES[names[market[0]]][1]
            key = 'batch:' + market[0] + ':' + raw.strip()
            batch = current.get(key)
            if batch is None:
                label = code + '-' + raw.strip()
                batch = self.insert('order_batches', {'__key': key, '__label': label,
                    'batch_number': label, 'source_batch_label': raw, 'default_market': {'id': market[0]}})
                current[key] = batch
            self.patch('order_lines', row, {'batch': {'id': batch['id']}})

    def refresh_months(self, orders, market_names):
        groups, gaps = rules['aggregate_orders'](orders)
        rows = self.rows('monthly_overview')
        existing = {}
        for row in rows:
            month = self.get(row, 'monthly_overview', 'month')
            ms = linked(self.get(row, 'monthly_overview', 'market'))
            skus = linked(self.get(row, 'monthly_overview', 'sku'))
            if not month or len(ms) != 1 or len(skus) != 1:
                self.patch('monthly_overview', row, {'coverage_note': '请填写月份、地区市场和产品SKU；本行尚未纳入汇总'})
                continue
            dt.datetime.strptime(month, '%Y-%m')
            key = (month, ms[0], skus[0])
            if key in existing:
                # Never publish duplicated forecast/actual aggregates after a user adds a duplicate row.
                raise ValueError('Duplicate month/market/SKU summary rows; merge before refresh.')
            existing[key] = row
        skus = {r['id']: self.get(r, 'skus', 'part_number') or r.get('name') for r in self.rows('skus')}
        now = dt.datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
        for key in sorted(set(groups) | set(existing)):
            month, market, sku = key
            group = groups.get(key, {'quantity': 0, 'picked': 0, 'pending': 0, 'total': 0,
                                     'missing_price': 0, 'invalid': 0, 'records': []})
            notes = ['实际按下单月份统计；提货量为该月订单的累计进度。']
            if gaps.get(market):
                notes.append(f'本市场有{gaps[market]}条订单缺日期或SKU，实际合计暂留空；可查看关联订单中的已登记部分。')
            if group['invalid']:
                notes.append(f'{group["invalid"]}条订单数量或渠道状态需确认。')
            if group['missing_price']:
                notes.append(f'{group["missing_price"]}条订单缺美元单价，订单总价留空。')
            values = {'__label': f'{month} {market_names.get(market, "")} {skus.get(sku, "")}',
                'month': month, 'market': {'id': market}, 'sku': {'id': sku},
                'known_quantity': group['quantity'], 'known_picked': group['picked'],
                'known_pending': group['pending'], 'known_value': group['total'],
                'date_gap': gaps.get(market, 0), 'invalid_count': group['invalid'],
                'missing_price': group['missing_price'], 'coverage_note': '\n'.join(notes),
                'orders': [{'id': rid} for rid in sorted(group['records'])] or None}
            if key in existing:
                row = existing[key]
                changed = any(row['fields'].get(self.fields['monthly_overview'][k]) != v
                    for k, v in values.items() if k not in {'market', 'sku', 'orders'})
                if changed or not self.get(row, 'monthly_overview', 'refreshed_at'):
                    values['refreshed_at'] = now
                self.patch('monthly_overview', row, values)
            else:
                self.insert('monthly_overview', {'__key': '|'.join(key), 'refreshed_at': now, **values})

    def validate_minimal(self):
        if set(self.tables) != MINIMAL_TABLES or set(self.tables.values()) != self.active:
            raise ValueError('The seven-table structure has changed; review mappings before refreshing. Removed tables will not be recreated.')

    def layouts(self):
        order = ['__label', 'order_number', 'market', 'business_owner', 'customer', 'sku', 'order_date', 'batch',
                 'channel_status', 'admin_status', 'order_quantity', 'pending_quantity', 'picked_quantity',
                 'unit_price', 'order_value', 'followup_text', 'automation_note',
                 'source_pickup_note', 'source_delivery_note', 'source_notes']
        summary = ['__label', 'month', 'market', 'business_owner', 'sku', 'product_name', 'forecast_quantity',
                   'actual_quantity', 'pending_quantity', 'picked_quantity', 'forecast_price',
                   'forecast_value', 'actual_value', 'coverage_note', 'orders']
        configs = [('order_lines', order), ('monthly_overview', summary),
                   ('order_batches', ['__label', 'batch_number', 'default_market', 'default_customer', 'source_batch_label'])]
        minimal = self.state.get('structure_version') == 'minimal-7-v1'
        if minimal:
            order = [k for k in order if not k.startswith('source_')]
            configs[0] = ('order_lines', order)
            configs += [
                ('skus', ['__label', 'brand', 'part_number', 'description_zh', 'ean_code', 'category', 'tags', 'tag_notes']),
                ('customers', ['__label', 'name', 'markets', 'business_owner', 'preferred_tags', 'preference_notes', 'state', 'notes']),
                ('markets', ['__label', 'name', 'code', 'region', 'business_owner', 'active']),
                ('tags', ['__label', 'name', 'group', 'definition', 'applicable_to', 'active'])]
        source_columns = ['__label', 'order_number', 'source_sheet', 'source_row_number', 'source_batch',
            'source_status', 'source_order_date', 'source_quantity', 'source_picked', 'source_pending',
            'source_ean', 'estimated_price', 'source_pickup_note', 'source_delivery_note', 'source_notes',
            'source_provenance', 'source_plan_history']
        for table, shown in configs:
            tid = self.tables[table]
            views = self.api.call('GET', f'/table/{tid}/view')
            byname = {v['name']: v for v in views}
            desired = [('日期待补', {'conjunction': 'and', 'filterSet': [
                {'fieldId': self.fields[table]['order_date'], 'operator': 'isEmpty', 'value': None}]})] if table == 'order_lines' else []
            if table == 'order_lines':
                desired += [(name + '订单', {'conjunction': 'and', 'filterSet': [
                    {'fieldId': self.fields[table]['country_code'], 'operator': 'is', 'value': code}]})
                    for name, (_, code) in COUNTRIES.items()]
                if minimal:
                    desired.append(('原始资料', None))
            if table == 'monthly_overview':
                month = dt.datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m')
                desired = [(n, {'conjunction': 'and', 'filterSet': [
                    {'fieldId': self.fields[table]['month'], 'operator': 'is', 'value': m}]})
                    for n, m in [('当月概览', month), ('2026年10月', '2026-10'), ('2026年11月', '2026-11')]]
                desired.append(('全部月份', None))
            for name, filter_ in desired:
                if name not in byname:
                    v = self.api.call('POST', f'/table/{tid}/view', {'name': name, 'type': 'grid', 'filter': filter_})
                    byname[name] = v
                v = byname[name]
                if v.get('filter') != filter_:
                    self.api.call('PUT', f'/table/{tid}/view/{v["id"]}/filter', {'filter': filter_})
                self.state.setdefault('operations_views', {})[name] = v['id']
            for view in byname.values():
                if view.get('type') != 'grid':
                    continue
                columns = source_columns if table == 'order_lines' and view['name'] == '原始资料' else shown
                meta = [{'fieldId': fid, 'columnMeta': {'order': columns.index(k) if k in columns else 100 + i,
                    'hidden': k not in columns, 'width': 210 if k in {'followup_text', 'coverage_note', 'source_provenance', 'source_plan_history'} else 150}}
                    for i, (k, fid) in enumerate(self.fields[table].items())]
                self.api.call('PUT', f'/table/{tid}/view/{view["id"]}/column-meta', meta)
        self.persist()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, required=True)
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--setup', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    with (args.folder / 'operations.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Refresh already running; skipped.')
            return
        op = Operations(args.folder, args.token_file)
        if args.setup:
            op.setup()
        op.refresh()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Credentials and arbitrary backend response bodies must never enter cron logs.
        print(type(error).__name__ + ': ' + str(error), file=sys.stderr)
        raise SystemExit(1)
