import runpy
from pathlib import Path
import unittest

retired=runpy.run_path(str(Path(__file__).resolve().parents[1]/'tools/schema/source_cleanup.py'))['retired']


class SourceCleanupTests(unittest.TestCase):
    def test_current_order_business_and_identifiers_are_protected(self):
        for key in ['order_number','number_prefix','country_sequence','country_code','region_code',
                    'order_date','order_quantity','picked_quantity','unit_price','pending_quantity','order_value',
                    'channel_status','admin_status','followup_text','requirements','market','sku','customer','batch']:
            self.assertFalse(retired('order_lines',key),key)

    def test_source_columns_and_embedded_raw_history_are_retired(self):
        for key in ['source_quantity','source_picked','source_pending','source_provenance',
                    'source_plan_history','source_batch','source_sheet','source_notes','__key']:
            self.assertTrue(retired('order_lines',key),key)

    def test_forecasts_and_current_customer_product_information_stay(self):
        for table,keys in {'monthly_overview':['forecast_quantity','forecast_price','actual_quantity','orders'],
                           'customers':['name','preferred_tags','preference_notes'],
                           'skus':['part_number','ean_code','tags','description_zh'],
                           'markets':['name','code','region','sequence_highwater']}.items():
            for key in keys: self.assertFalse(retired(table,key),(table,key))

    def test_unmapped_user_added_fields_are_not_deleted(self):
        self.assertFalse(retired('markets',None))
        self.assertFalse(retired('order_lines','custom_business_field'))


if __name__=='__main__': unittest.main()
