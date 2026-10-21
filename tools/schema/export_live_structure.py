"""Export only table/field/view definitions; never records or credentials.

Run on the Ubuntu server with the private import state and automation token.
The resulting JSON is a structural snapshot, not a schema deployment program.
"""
import argparse
import datetime as dt
import json
from pathlib import Path
import runpy


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--folder', required=True, type=Path)
    p.add_argument('--token-file', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    args = p.parse_args()
    helper = runpy.run_path(str(args.folder / 'import-teable-orders.py'))
    state = json.loads((args.folder / 'state.json').read_text())
    api = helper['Api']('http://127.0.0.1:3000', args.token_file.read_text().strip())
    names = {v: k for k, v in state['tables'].items()}
    model = {'version': '3.4' if state.get('source_storage_version') == 'removed-v1' else '3.3' if state.get('order_primary_version') else '3.2' if state.get('batch_name_version') else '3.1' if state.get('structure_version') == 'minimal-7-v1' else '3.0',
        'structure_version': state.get('structure_version'), 'snapshot_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'deployment_mode': 'structure_snapshot_only', 'tables': [],
        'rules': {'forecast_owner': '渠道业务', 'forecast_source': '2026年10月、11月计划列',
            'currency': 'USD', 'order_number': '大区+真实下单日期YYYYMMDD+国家+国家独立连续6位序号',
            'number_grain': '每条SKU订单明细', 'batch_name_unique': False,
            'batch_name': '大区码+批次下单日期YYYYMMDD+国家码+关联订单实际订单数量之和',
            'batch_date': '明确填写的批次下单日期优先；仅全部关联订单日期一致时自动补空值',
            'batch_missing_date': '显示待补日期，不取导入日或猜测最早/最晚日期',
            'missing_order_date': '不生成订单号，不猜测月份',
            'country_codes': {'BRA': 'SA', 'ARG': 'SA', 'VNM': 'SEA', 'PHL': 'SEA', 'HKG': 'SEA', 'POL': 'EU', 'TUR': 'EU'},
            'order_primary_field': '订单号；缺真实下单日期时留空',
            'pickup': '渠道业务从基地或仓库提走；不是客户交付',
            'monthly_actual': '按真实下单月归组；提货为这些订单的累计进度',
            'missing_price': '金额留空，零价格有效',
            'source_storage': '业务库不保留原始导入资料；逐次记录变更独立备份' if state.get('source_storage_version') == 'removed-v1' else '原始导入资料暂存于隐藏字段'}}
    for table in api.call('GET', f'/base/{state["base_id"]}/table'):
        key = names[table['id']]
        reverse = {v: k for k, v in state['fields'][key].items()}
        item = {'key': key, 'name': table['name'], 'fields': [], 'views': []}
        for f in api.call('GET', f'/table/{table["id"]}/field'):
            entry = {'key': reverse.get(f['id'], f['name']), 'name': f['name'], 'type': f['type'],
                     'primary': bool(f.get('isPrimary')), 'computed': bool(f.get('isComputed') or f.get('isLookup'))}
            options = f.get('options') or {}
            if 'choices' in options:
                entry['choices'] = [c['name'] for c in options['choices']]
            if f['type'] == 'link':
                entry.update(target=names.get(options.get('foreignTableId')), relationship=options.get('relationship'))
            if f.get('isLookup'):
                look = f['lookupOptions']
                foreign = names[look['foreignTableId']]
                mapping = {v: k for k, v in state['fields'][foreign].items()}
                entry['lookup'] = {'target': foreign, 'source': mapping.get(look['lookupFieldId'])}
            item['fields'].append(entry)
        item['views'] = [v['name'] for v in api.call('GET', f'/table/{table["id"]}/view')]
        model['tables'].append(item)
    args.output.write_text(json.dumps(model, ensure_ascii=False, indent=2) + '\n')
    print('Exported structure only:', len(model['tables']), 'tables')


if __name__ == '__main__':
    main()
