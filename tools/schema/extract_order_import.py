"""Read the seven requested XLSX order sheets without modifying or interpreting styles.

Writes private JSON only to an explicitly supplied path. Does not connect to Teable.
"""
import argparse
from collections import Counter
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile

NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
RID = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
MARKETS = ['巴西', '阿根廷', '波兰', '土耳其', '越南', '香港', '菲律宾']
REGIONS = {'巴西': 'SA', '阿根廷': 'SA', '波兰': 'EU', '土耳其': 'EU', '越南': 'SEA', '菲律宾': 'SEA', '香港': None}
ALIASES = {
    '序号': 'sequence', '批次': 'batch', '品牌': 'brand', '品类': 'category',
    '料号': 'part', 'EAN码': 'ean', '客户描述/要求': 'requirements', '型号': 'model',
    '下单时间': 'order_date', 'FCST提交时间': 'forecast_date', '计划交付时间': 'planned_date',
    '数量': 'quantity', '已发货': 'picked', '已提货': 'picked', '待发货': 'pending', '待提货': 'pending',
    '预估单价': 'estimated_price', '业务提货记录': 'pickup_note', '客户交付记录': 'delivery_note',
    '订单状态': 'source_status', '相关负责人': 'owner_raw', '备品': 'spares_raw',
}
SENSITIVE = re.compile(r'密码|口令|password|登录账号|登录帐号', re.I)


def safe(value):
    if isinstance(value, str) and SENSITIVE.search(value):
        return '[excluded sensitive note]'
    return value


def numeric(value):
    if value is None or value == '':
        return None
    try:
        n = Decimal(str(value))
    except InvalidOperation:
        return None
    if not n.is_finite():
        return None
    return int(n) if n == n.to_integral_value() else float(n)


def read_cells(root, strings):
    result = {}
    for c in root.findall('s:sheetData/s:row/s:c', NS):
        value = c.find('s:v', NS)
        value = value.text if value is not None else None
        kind = c.attrib.get('t', 'n')
        if kind == 's' and value is not None:
            value = strings[int(value)]
        elif kind == 'inlineStr':
            value = ''.join(t.text or '' for t in c.findall('.//s:t', NS))
        elif kind == 'n':
            value = numeric(value)
        formula = c.find('s:f', NS)
        result[c.attrib['r']] = {'value': safe(value), 'formula': formula.text if formula is not None else None,
                                'kind': kind, 'style': c.attrib.get('s')}
    return result


def parse_date(cell, date1904=False):
    """Only called for explicitly date-labelled columns. Raw values are also retained."""
    if cell is None or cell['value'] is None:
        return None
    value = cell['value']
    if isinstance(value, (int, float)) and 1 <= value <= 100000:
        epoch = dt.datetime(1904, 1, 1) if date1904 else dt.datetime(1899, 12, 30)
        return (epoch + dt.timedelta(days=value)).date().isoformat()
    if isinstance(value, str):
        for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d', '%Y-%m-%d %H:%M:%S'):
            try:
                return dt.datetime.strptime(value, fmt).date().isoformat()
            except ValueError:
                pass
    return None


def extract(path):
    path = Path(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    output = {'filename': path.name, 'sha256': digest, 'mapping_version': 'orders-7sheets-v1',
              'sheets': [], 'rows': [], 'shared_notes': []}
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read('xl/workbook.xml'))
        props = workbook.find('s:workbookPr', NS)
        date1904 = props is not None and props.attrib.get('date1904') in {'1', 'true'}
        strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            strings = [''.join(n.text or '' for n in si.findall('.//s:t', NS))
                       for si in ET.fromstring(archive.read('xl/sharedStrings.xml'))]
        relations = {r.attrib['Id']: r.attrib['Target']
                     for r in ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))}
        selected = {}
        for sheet in workbook.findall('s:sheets/s:sheet', NS):
            market = re.sub(r'\s*\([^)]*\)\s*$', '', sheet.attrib['name']).strip()
            if market in MARKETS:
                if market in selected:
                    raise ValueError('Duplicate target market sheet: ' + market)
                selected[market] = sheet
        if set(selected) != set(MARKETS):
            raise ValueError('Missing requested market sheets: ' + ', '.join(set(MARKETS) - set(selected)))
        for market in MARKETS:
            sheet = selected[market]
            target = relations[sheet.attrib[RID]]
            member = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
            cells = read_cells(ET.fromstring(archive.read(member)), strings)
            columns, raw_headers = {}, {}
            for address, cell in cells.items():
                col, row = re.fullmatch(r'([A-Z]+)(\d+)', address).groups()
                if row != '1' or not cell['value']:
                    continue
                header = str(cell['value']).strip()
                normalized = re.sub(r'\s+', '', header)
                if normalized in ALIASES or '备注' in normalized or re.fullmatch(r'\d{1,2}月(?:更新)?', normalized):
                    raw_headers[col] = header
                    key = ALIASES.get(normalized, 'note:' + header)
                    if key in columns:
                        raise ValueError('Duplicate mapped header: ' + sheet.attrib['name'] + '/' + key)
                    columns[key] = col
            if not {'part', 'model', 'quantity', 'picked', 'pending', 'source_status'} <= set(columns):
                raise ValueError('Required headers missing in ' + sheet.attrib['name'])
            rows = sorted({int(re.search(r'\d+$', a).group()) for a in cells})
            before = len(output['rows'])
            for rownum in rows:
                if rownum <= 1:
                    continue
                selected_cells = {k: cells.get(f'{col}{rownum}', {'value': None, 'formula': None})
                                  for k, col in columns.items()}
                values = {k: c['value'] for k, c in selected_cells.items()}
                if not (values.get('part') or values.get('model')):
                    continue  # Never import total/formula-only/footer rows.
                if str(values.get('part', '')).strip() in {'合计', '总计', '汇总'}:
                    continue
                issues = ['客户及正式批次归属待核对', '历史数量及期初待核对']
                quantities = {k: numeric(values.get(k)) for k in ('quantity', 'picked', 'pending')}
                for key, n in quantities.items():
                    if n is None:
                        issues.append(key + '数量缺失或公式无缓存')
                    elif n < 0:
                        issues.append(key + '数量为负')
                q, picked, pending = [quantities[k] for k in ('quantity', 'picked', 'pending')]
                if None not in (q, picked, pending) and Decimal(str(q)) != Decimal(str(picked)) + Decimal(str(pending)):
                    issues.append('源数量不等于已提货加待提货')
                if values.get('source_status') == '已完结' and pending and pending > 0:
                    issues.append('源已完结但仍有待提货')
                if market == '香港':
                    issues.append('香港业务大区归属待核对')
                part = str(values['part']).strip() if values.get('part') else None
                if not part:
                    issues.append('缺少正式料号')
                dates = {key: parse_date(selected_cells.get(key), date1904)
                         for key in ('order_date', 'forecast_date', 'planned_date')}
                for key, val in dates.items():
                    if values.get(key) is not None and val is None:
                        issues.append(key + '日期原文待核对')
                output['rows'].append({'source_key': f'{digest}:{sheet.attrib["name"]}:{rownum}',
                    'sheet': sheet.attrib['name'], 'row': rownum, 'market': market, 'region': REGIONS[market],
                    'values': values, 'quantities': quantities, 'dates': dates, 'part': part,
                    'cells': {columns[k]: c for k, c in selected_cells.items()}, 'issues': issues})
            row_numbers = {r['row'] for r in output['rows'][before:]}
            root = ET.fromstring(archive.read(member))
            for merge in root.findall('s:mergeCells/s:mergeCell', NS):
                match = re.fullmatch(r'([A-Z]+)(\d+):([A-Z]+)(\d+)', merge.attrib['ref'])
                if not match:
                    continue
                col, first, endcol, last = match.groups()
                if col != endcol or col not in raw_headers:
                    continue
                key = next(k for k, c in columns.items() if c == col)
                if key not in {'pickup_note', 'delivery_note'} and not key.startswith('note:'):
                    continue
                text = cells.get(f'{col}{first}', {}).get('value')
                covered = sorted(row_numbers & set(range(int(first), int(last) + 1)))
                if text and covered:
                    output['shared_notes'].append({'sheet': sheet.attrib['name'], 'range': merge.attrib['ref'],
                        'header': raw_headers[col], 'text': str(text), 'rows': covered})
            output['sheets'].append({'market': market, 'sheet': sheet.attrib['name'],
                'row_count': len(output['rows']) - before, 'headers': raw_headers})
    output['summary'] = {'row_count': len(output['rows']), 'by_market': dict(Counter(r['market'] for r in output['rows'])),
        'totals': {k: sum(Decimal(str(r['quantities'][k])) for r in output['rows'] if r['quantities'][k] is not None)
                   for k in ('quantity', 'picked', 'pending')},
        'missing_quantities': sum(any(n is None for n in r['quantities'].values()) for r in output['rows'])}
    output['summary']['totals'] = {k: str(v) for k, v in output['summary']['totals'].items()}
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workbook', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    data = extract(args.workbook)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'sha256': data['sha256'], **data['summary']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
