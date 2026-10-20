"""Business counterexamples for the proposed model; no live service or source data is used."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('order_rules', ROOT / 'tools/schema/order_rules.py')
rules = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rules)


def pickup(key, quantity):
    return {'id': key, 'kind': 'pickup', 'quantity': quantity}


class QuantityCounterexamples(unittest.TestCase):
    def test_partial_pickup_does_not_depend_on_planned_month(self):
        result = rules.summarize(1000, [pickup('oct', 600), pickup('dec', 300)])
        self.assertEqual(result['gross_picked'], 900)
        self.assertEqual(result['to_pick'], 100)

    def test_short_close_is_not_extra_pickup(self):
        result = rules.summarize(1000, [pickup('a', 900)], closed_quantity=100)
        self.assertEqual(result['to_pick'], 0)
        self.assertEqual(result['net_picked'], 900)
        self.assertEqual(result['closed'], 100)

    def test_return_for_replacement_reopens_demand(self):
        result = rules.summarize(1000, [pickup('a', 900),
            {'id': 'r', 'kind': 'return', 'quantity': 50, 'against': 'a', 'resolution': 'reopen'}])
        self.assertEqual(result['net_picked'], 850)
        self.assertEqual(result['to_pick'], 150)

    def test_return_without_replacement_does_not_reopen_demand(self):
        result = rules.summarize(1000, [pickup('a', 900),
            {'id': 'r', 'kind': 'return', 'quantity': 50, 'against': 'a', 'resolution': 'no_reopen'}])
        self.assertEqual(result['net_picked'], 850)
        self.assertEqual(result['to_pick'], 100)

    def test_replacement_can_raise_gross_pickup_without_over_fulfilling(self):
        result = rules.summarize(1000, [pickup('a', 1000),
            {'id': 'r', 'kind': 'return', 'quantity': 50, 'against': 'a', 'resolution': 'reopen'},
            pickup('replacement', 50)])
        self.assertEqual(result['gross_picked'], 1050)
        self.assertEqual(result['net_picked'], 1000)
        self.assertEqual(result['to_pick'], 0)

    def test_unresolved_return_is_not_a_reliable_zero(self):
        result = rules.summarize(1000, [pickup('a', 900),
            {'id': 'r', 'kind': 'return', 'quantity': 50, 'against': 'a', 'resolution': 'pending'}])
        self.assertIsNone(result['to_pick'])

    def test_reversal_keeps_original_but_cancels_its_effect_once(self):
        events = [pickup('a', 600), {'id': 'reverse', 'kind': 'reversal', 'quantity': 600, 'against': 'a'}]
        self.assertEqual(rules.summarize(1000, events)['to_pick'], 1000)
        with self.assertRaises(ValueError):
            rules.summarize(1000, events + [{'id': 'again', 'kind': 'reversal', 'quantity': 600, 'against': 'a'}])

    def test_reversed_unresolved_return_no_longer_blocks_balance(self):
        events = [pickup('a', 900),
                  {'id': 'r', 'kind': 'return', 'quantity': 50, 'against': 'a', 'resolution': 'pending'},
                  {'id': 'reverse-r', 'kind': 'reversal', 'quantity': 50, 'against': 'r'}]
        result = rules.summarize(1000, events)
        self.assertEqual(result['net_picked'], 900)
        self.assertEqual(result['to_pick'], 100)

    def test_duplicate_posting_and_excess_pickup_are_rejected(self):
        for events in [[pickup('a', 600), pickup('a', 600)], [pickup('a', 1001)]]:
            with self.subTest(events=events), self.assertRaises(ValueError):
                rules.summarize(1000, events)

    def test_excess_return_cannot_create_negative_physical_quantity(self):
        with self.assertRaises(ValueError):
            rules.summarize(1000, [pickup('a', 100),
                {'id': 'r', 'kind': 'return', 'quantity': 101, 'against': 'a', 'resolution': 'reopen'}])

    def test_box_conversion_uses_confirmed_snapshot(self):
        self.assertEqual(rules.to_base_quantity(3, 40, confirmed=True), 120)
        with self.assertRaises(ValueError):
            rules.to_base_quantity(3, 40, confirmed=False)

    def test_one_physical_pickup_can_serve_multiple_batches(self):
        allocations = [dict(id='x', sku='SKU', channel='C', order_line='batch-a-line', quantity=600),
                       dict(id='y', sku='SKU', channel='C', order_line='batch-b-line', quantity=300)]
        rules.validate_allocations(900, 'SKU', 'C', allocations)
        with self.assertRaises(ValueError):
            rules.validate_allocations(899, 'SKU', 'C', allocations)

    def test_wrong_sku_channel_or_plan_cannot_be_allocated(self):
        for patch in [dict(sku='OTHER'), dict(channel='OTHER'), dict(plan_order_line='OTHER')]:
            row = dict(id='x', sku='SKU', channel='C', order_line='line', quantity=100)
            row.update(patch)
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                rules.validate_allocations(100, 'SKU', 'C', [row])


if __name__ == '__main__':
    unittest.main()
