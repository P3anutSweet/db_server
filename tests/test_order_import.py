"""Synthetic import regressions: column drift, broken styles, cached formulas and safe reruns."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


extractor = module('extract_orders', 'tools/schema/extract_order_import.py')
loader = module('import_orders', 'scripts/import-teable-orders.py')


def fixture(path, price_column=False, no_cache=False):
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    headers = {'E': '料号', 'H': '型号', 'I': '下单时间', 'L': '数量', 'M': '已发货', 'N': '待发货'}
    headers.update({'O': '预估单价', 'P': '业务提货记录', 'R': '订单状态'} if price_column
                   else {'O': '业务提货记录', 'Q': '订单状态'})
    cell = lambda address, value: f'<c r="{address}" t="inlineStr"><is><t>{value}</t></is></c>'
    names = [name + ' (test)' for name in extractor.MARKETS]
    sheets = ''.join(f'<sheet name="{name}" sheetId="{i}" r:id="r{i}"/>' for i, name in enumerate(names, 1))
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('xl/workbook.xml', f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{sheets}</sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships>' + ''.join(f'<Relationship Id="r{i}" Target="worksheets/sheet{i}.xml"/>' for i in range(1, 8)) + '</Relationships>')
        archive.writestr('xl/styles.xml', '<broken-style-that-must-not-be-parsed/>')
        for i in range(1, 8):
            head = ''.join(cell(c+'1', v) for c, v in headers.items())
            qty = '<c r="L2"><f>50+50</f>' + ('' if no_cache else '<v>100</v>') + '</c>'
            note_col, state_col = ('P', 'R') if price_column else ('O', 'Q')
            row = cell('E2', 'SKU001') + cell('H2', 'Keyboard') + '<c r="I2"><v>46121</v></c>' + qty
            row += '<c r="M2"><v>30</v></c><c r="N2"><v>70</v></c>' + cell(note_col+'2', 'pickup note') + cell(state_col+'2', '已完结')
            if price_column:
                row += '<c r="O2"><v>12.5</v></c>'
            archive.writestr(f'xl/worksheets/sheet{i}.xml', f'<worksheet xmlns="{ns}"><sheetData><row r="1">{head}</row><row r="2">{row}</row></sheetData></worksheet>')


class ExtractTests(unittest.TestCase):
    def test_new_price_column_does_not_shift_notes_or_status(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / 'fixture.xlsx'
            fixture(path, price_column=True)
            data = extractor.extract(path)
        self.assertEqual(len(data['rows']), 7)
        row = data['rows'][0]
        self.assertEqual(row['values']['estimated_price'], 12.5)
        self.assertEqual(row['values']['pickup_note'], 'pickup note')
        self.assertEqual(row['values']['source_status'], '已完结')
        self.assertEqual(row['dates']['order_date'], '2026-04-09')
        self.assertIn('源已完结但仍有待提货', row['issues'])

    def test_old_layout_and_corrupt_styles_are_read_without_changes(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / 'fixture.xlsx'
            fixture(path)
            before = path.read_bytes()
            data = extractor.extract(path)
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(data['rows'][0]['values']['pickup_note'], 'pickup note')
        self.assertNotIn('estimated_price', data['rows'][0]['values'])
        self.assertEqual(data['summary']['totals']['quantity'], '700')

    def test_missing_formula_cache_is_unknown_not_zero(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / 'fixture.xlsx'
            fixture(path, no_cache=True)
            data = extractor.extract(path)
        self.assertIsNone(data['rows'][0]['quantities']['quantity'])
        self.assertEqual(data['summary']['missing_quantities'], 7)
        self.assertEqual(data['rows'][0]['cells']['L']['formula'], '50+50')

    def test_hong_kong_region_is_not_invented(self):
        self.assertIsNone(extractor.REGIONS['香港'])
        self.assertEqual(extractor.parse_date({'value': 1}, True), '1904-01-02')


class FakeApi:
    def __init__(self):
        self.rows = []
        self.posts = 0

    def records(self, table):
        return self.rows

    def call(self, method, route, payload):
        assert method == 'POST'
        self.posts += 1
        record = {'id': 'recExample', 'fields': payload['records'][0]['fields']}
        self.rows.append(record)
        return {'records': [record]}


class ResumeTests(unittest.TestCase):
    def test_retry_and_restart_do_not_duplicate_or_overwrite_user_edits(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / 'state.json'
            api = FakeApi()
            data = {'sha256': 'same-file'}
            run = loader.Importer(api, {}, data, path, 'space1')
            run.state['tables'] = {'order_lines': 'table1'}
            run.state['fields'] = {'order_lines': {'__key': 'key', '__label': 'label', 'source_quantity': 'quantity'}}
            first = run.record('order_lines', 'source1', 'row', {'source_quantity': 100})
            api.rows[0]['fields']['quantity'] = 90
            self.assertEqual(run.record('order_lines', 'source1', 'row', {'source_quantity': 100}), first)
            run.persist()
            second = loader.Importer(api, {}, data, path, 'space1')
            self.assertEqual(second.record('order_lines', 'source1', 'row', {'source_quantity': 100}), first)
            self.assertEqual(api.posts, 1)
            self.assertEqual(api.rows[0]['fields']['quantity'], 90)

    def test_different_snapshot_cannot_reuse_state(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / 'state.json'
            path.write_text(json.dumps({'source_sha256': 'old', 'space_id': 'space1'}))
            with self.assertRaises(ValueError):
                loader.Importer(FakeApi(), {}, {'sha256': 'new'}, path, 'space1')


if __name__ == '__main__':
    unittest.main()
