import importlib.util
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

spec = importlib.util.spec_from_file_location('order_numbers', Path(__file__).resolve().parents[1] / 'tools/schema/order_numbers.py')
rules = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rules)


class OperationsTests(unittest.TestCase):
    def test_canonical_country_codes(self):
        day = rules.business_day('2026-10-20')
        self.assertEqual(rules.order_number('SEA', 'VNM', day, 5), 'SEA20261020VNM000005')
        self.assertEqual(rules.order_number('SEA', 'PHL', day, 23), 'SEA20261020PHL000023')
        self.assertEqual(rules.batch_name_details([self.batch_row(market_name='越南')])['name'], 'SEA20261020VNM1000')
        for old in ['VIE', 'PHI']:
            with self.assertRaises(ValueError):
                rules.order_number('SEA', old, day, 1)

    def test_rename_country_preserves_reservations_and_durable_floor(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp:
            c = rules.CountryCounter(Path(tmp) / 'counter.db')
            c.reserve('VIE', 'old', 5)
            c.floor('VIE', 12)
            c.rename_country('VIE', 'VNM')
            c.rename_country('VIE', 'VNM')
            self.assertEqual(c.reserve('VNM', 'old'), 5)
            self.assertEqual(c.reserve('VNM', 'next'), 13)
            with c.connect() as db:
                self.assertIsNone(db.execute('SELECT value FROM counters WHERE country="VIE"').fetchone())

    def test_rename_collision_rolls_back(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp:
            c = rules.CountryCounter(Path(tmp) / 'counter.db')
            c.reserve('PHI', 'old', 3)
            c.reserve('PHL', 'new', 3)
            with self.assertRaises(ValueError):
                c.rename_country('PHI', 'PHL')
            self.assertEqual(c.reserve('PHI', 'old'), 3)
            self.assertEqual(c.reserve('PHL', 'new'), 3)

    def batch_row(self, **changes):
        return {'market_name':'巴西', 'order_date':'2026-10-20', 'order_quantity':1000, **changes}

    def test_batch_name_uses_region_date_country_and_linked_order_quantity(self):
        result=rules.batch_name_details([self.batch_row(),self.batch_row(order_quantity=250)])
        self.assertEqual(result['name'],'SA20261020BRA1250')
        self.assertTrue(result['inferred_date'])

    def test_batch_date_is_not_guessed_from_partial_or_multiple_dates(self):
        for rows in [[self.batch_row(),self.batch_row(order_date=None)],
                     [self.batch_row(),self.batch_row(order_date='2026-11-01')]]:
            result=rules.batch_name_details(rows)
            self.assertIsNone(result['date'])
            self.assertIn('待补日期',result['name'])

    def test_explicit_batch_date_supports_cross_month_orders_and_shanghai_time(self):
        result=rules.batch_name_details([self.batch_row(),self.batch_row(order_date='2026-11-01')],
            explicit_date='2026-10-19T16:00:00Z')
        self.assertEqual(result['name'],'SA20261020BRA2000')
        self.assertFalse(result['inferred_date'])

    def test_batch_missing_quantity_is_not_a_partial_or_zero_total(self):
        result=rules.batch_name_details([self.batch_row(),self.batch_row(order_quantity=None)])
        self.assertIsNone(result['total'])
        self.assertIn('待补件数',result['name'])
        empty=rules.batch_name_details([],explicit_date='2026-10-20',default_market='巴西')
        self.assertIsNone(empty['total'])

    def test_mixed_countries_do_not_choose_an_arbitrary_batch_code(self):
        result=rules.batch_name_details([self.batch_row(),self.batch_row(market_name='波兰')])
        self.assertIn('待补国家',result['name'])
        result=rules.batch_name_details([self.batch_row()],default_market='波兰')
        self.assertIn('待补国家',result['name'])

    def test_batch_quantity_changes_update_name_and_preserve_real_zero(self):
        self.assertEqual(rules.batch_name_details([self.batch_row(order_quantity=0)])['name'],'SA20261020BRA0')
        self.assertEqual(rules.batch_name_details([self.batch_row(order_quantity=1500)])['name'],'SA20261020BRA1500')

    def test_country_sequences_are_independent_and_persist(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp:
            p = Path(tmp) / 'counter.db'
            c = rules.CountryCounter(p)
            self.assertEqual(c.reserve('BRA', 'one'), 1)
            self.assertEqual(c.reserve('ARG', 'two'), 1)
            self.assertEqual(rules.CountryCounter(p).reserve('BRA', 'three'), 2)
            self.assertEqual(c.reserve('BRA', 'one'), 1)
            with self.assertRaises(ValueError):
                c.reserve('VNM', 'one')

    def test_concurrent_requests_do_not_duplicate_or_reuse(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp:
            c = rules.CountryCounter(Path(tmp) / 'counter.db')
            with ThreadPoolExecutor(max_workers=6) as pool:
                values = list(pool.map(lambda i: c.reserve('BRA', str(i)), range(30)))
            self.assertEqual(sorted(values), list(range(1, 31)))
            c.floor('BRA', 100)
            self.assertEqual(c.reserve('BRA', 'next'), 101)

    def test_six_digits_never_wrap(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp:
            c = rules.CountryCounter(Path(tmp) / 'counter.db')
            c.floor('PHL', 999999)
            with self.assertRaises(ValueError):
                c.reserve('PHL', 'overflow')

    def test_order_date_uses_shanghai_day(self):
        day = rules.business_day('2026-10-19T16:00:00.000Z')
        self.assertEqual(rules.order_number('SA', 'BRA', day, 1), 'SA20261020BRA000001')
        self.assertIsNone(rules.business_day(None))

    def test_null_price_is_not_zero(self):
        self.assertIsNone(rules.amount(5, None))
        self.assertEqual(rules.amount(5, 0), 0)
        self.assertEqual(rules.amount(3, 1.005), 3.02)

    def test_forecast_and_actual_have_different_months(self):
        row = dict(id='a', market='BRA', sku='s', order_date='2026-07-01',
                   channel_status='已下单', order_quantity=100, picked_quantity=30, unit_price=2)
        gs, gaps = rules.aggregate_orders([row, dict(row, id='b', channel_status='预测'),
                                          dict(row, id='c', order_date=None)])
        self.assertEqual(list(gs), [('2026-07', 'BRA', 's')])
        self.assertEqual(gs[('2026-07', 'BRA', 's')]['quantity'], 100)
        self.assertEqual(gs[('2026-07', 'BRA', 's')]['pending'], 70)
        self.assertEqual(gaps, {'BRA': 1})

    def test_partial_prices_and_invalid_pickup_do_not_report_complete_value(self):
        row = dict(id='a', market='BRA', sku='s', order_date='2026-07-01',
                   channel_status='已下单', order_quantity=10, picked_quantity=3, unit_price=2)
        gs, _ = rules.aggregate_orders([row, dict(row, id='b', unit_price=None)])
        self.assertIsNone(next(iter(gs.values()))['total'])
        gs, _ = rules.aggregate_orders([dict(row, picked_quantity=11)])
        self.assertEqual(next(iter(gs.values()))['invalid'], 1)


if __name__ == '__main__':
    unittest.main()
