import copy
import importlib.util
import json
import unittest
from pathlib import Path
from typing import Any

spec = importlib.util.spec_from_file_location('safe', Path(__file__).parents[1] / 'hypest_alibaba_safe.py')
assert spec is not None and spec.loader is not None
safe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(safe)
setattr(safe, 'MEMBER_ID', 'fixture-member')
setattr(safe, 'NAME', 'Fixture buyer')


class MergeTests(unittest.TestCase):
    def setUp(self):
        row = dict(order_id='33316484501049310', supplier='Fixture supplier', status='Fixture status', amount_raw='Total: USD 2894.00', order_date_raw='02 Oct 2026, GMT-07:00', list_raw_text='fixture', list_items=['summary'], list_items_complete=False)
        self.existing = {'orders': [dict(order_id=row['order_id'], items=['complete detail'], tracking='keep', paid_amount='123', unknown={'preserve': True})]}
        self.capture: dict[str, Any] = dict(account=dict(member_id=safe.MEMBER_ID, name=safe.NAME), captured_at='fixture', pages=[dict(page=1, pages=1, total=1, url=safe.LIST_URL, all=True, rows=[row])])

    def test_lossless(self):
        original = copy.deepcopy(self.existing)
        out = safe.merge(self.existing, self.capture)
        for key in ('items', 'tracking', 'paid_amount', 'unknown'):
            self.assertEqual(out['orders'][0][key], original['orders'][0][key])
        self.assertEqual(self.existing, original)

    def test_account_rejected(self):
        self.capture['account']['member_id'] = 'wrong'
        with self.assertRaises(ValueError): safe.merge(self.existing, self.capture)

    def test_missing_page_rejected(self):
        self.capture['pages'][0]['pages'] = 2
        with self.assertRaises(ValueError): safe.merge(self.existing, self.capture)

    def test_duplicate_rejected(self):
        self.capture['pages'][0]['rows'] *= 2
        self.capture['pages'][0]['total'] = 2
        with self.assertRaises(ValueError): safe.merge(self.existing, self.capture)

    def test_disappeared_order_rejected(self):
        self.existing['orders'].append({'order_id': '11111111111111111'})
        with self.assertRaises(ValueError): safe.merge(self.existing, self.capture)

    def test_filter_rejected(self):
        self.capture['pages'][0]['all'] = False
        with self.assertRaises(ValueError): safe.merge(self.existing, self.capture)

    def test_new_order_no_fabricated_details(self):
        out = safe.merge({'orders': []}, self.capture)['orders'][0]
        self.assertEqual(out['items'], [])
        self.assertIsNone(out['paid_amount'])
        self.assertEqual(out['list_items'], ['summary'])

    def test_iso_date(self):
        self.capture['pages'][0]['rows'][0]['order_date_raw'] = '2026-05-07'
        self.assertEqual(safe.merge(self.existing, self.capture)['orders'][0]['order_date'], '2026-05-07')


if __name__ == '__main__': unittest.main()
