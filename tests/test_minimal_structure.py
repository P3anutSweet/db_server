import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('minimal', ROOT / 'scripts/migrate-minimal-structure.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def fixture():
    result = {'state': {'fields': {}}, 'tables': {k: {'records': []} for k in m.KEEP + m.RETIRE}}
    def add(table, rid, fields):
        mapping = result['state']['fields'].setdefault(table, {})
        for key in fields:
            mapping[key] = 'f_' + table + '_' + key
        row = {'id': rid, 'fields': {mapping[k]: v for k,v in fields.items()}}
        result['tables'][table]['records'].append(row)
    for k in result['tables']:
        result['state']['fields'][k] = {}
    add('regions', 'region', {'code':'EU','name':'欧洲','business_owner':'owner'})
    add('markets', 'market', {'region':{'id':'region'},'name':'market','sequence_highwater':123})
    add('import_batches', 'snapshot', {'filename':'source.xlsx','sha256':'HASH','mapping_version':'v1'})
    add('source_rows', 'source', {'snapshot':{'id':'snapshot'},'sheet':'Sheet','row':2,
        'business_values':json.dumps({'formula':'=L2-M2','cached':3})})
    add('order_lines', 'order', {'source_rows':[{'id':'source'}],'order_number':'EU20261020POL000001','order_quantity':5})
    add('line_plans', 'plan', {'line':{'id':'order'},'state':'草稿',
        'plan_kind':'原计划节点待核对','__key':'source-plan:HASH:Sheet:2','planned_qty':5})
    return result


class MinimalStructureTests(unittest.TestCase):
    def test_full_source_snapshot_and_plan_are_embedded(self):
        p=m.prepare(fixture())
        source=json.loads(p['histories']['order']['source_provenance'])
        self.assertEqual(source['rows'][0]['import_snapshots'][0]['values']['sha256'],'HASH')
        self.assertEqual(json.loads(source['rows'][0]['values']['business_values'])['formula'],'=L2-M2')
        plans=json.loads(p['histories']['order']['source_plan_history'])
        self.assertEqual(plans['plans'][0]['values']['planned_qty'],5)
        self.assertEqual(p['markets']['market']['business_region_code'],'EU')
        self.assertIn('owner',p['markets']['market']['region_provenance'])

    def test_unmapped_sources_cannot_be_discarded(self):
        s=fixture(); extra=copy.deepcopy(s['tables']['source_rows']['records'][0]);extra['id']='unmapped';s['tables']['source_rows']['records'].append(extra)
        with self.assertRaisesRegex(ValueError,'orphaned'):
            m.prepare(s)

    def test_operational_plans_are_not_mistaken_for_imported_drafts(self):
        s=fixture();fid=s['state']['fields']['line_plans']['state'];s['tables']['line_plans']['records'][0]['fields'][fid]='有效'
        with self.assertRaisesRegex(ValueError,'operational business data'):
            m.prepare(s)

    def test_new_per_tag_metadata_blocks_empty_table_retirement(self):
        s=fixture();s['tables']['customer_preferences']['records']=[{'id':'new','fields':{}}]
        with self.assertRaisesRegex(ValueError,'per-tag metadata'):
            m.prepare(s)

    def test_a_plan_with_a_missing_order_is_not_dropped(self):
        s=fixture();fid=s['state']['fields']['line_plans']['line'];s['tables']['line_plans']['records'][0]['fields'][fid]={'id':'missing'}
        with self.assertRaisesRegex(ValueError,'unambiguous existing order'):
            m.prepare(s)

    def test_existing_identifiers_and_quantities_are_protected(self):
        s=fixture()
        class Op:
            def rows(self, key): return copy.deepcopy(s['tables'][key]['records'])
        op=Op();m.verify_business(op,s)
        changed=copy.deepcopy(s)
        fid=s['state']['fields']['order_lines']['order_quantity']
        changed['tables']['order_lines']['records'][0]['fields'][fid]=6
        with self.assertRaisesRegex(ValueError,'business value changed'):
            m.verify_business(op,changed)

    def test_plan_mode_never_opens_a_token_or_connects(self):
        r=subprocess.run([sys.executable,str(ROOT/'scripts/migrate-minimal-structure.py'),
            '--folder','nonexistent','--token-file','nonexistent'],capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout)['mode'],'plan_only')


if __name__ == '__main__':
    unittest.main()
