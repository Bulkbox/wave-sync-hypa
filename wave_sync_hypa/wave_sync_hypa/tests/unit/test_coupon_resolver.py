"""Unit tests for resolvers.coupon_resolver.find_coupon_code (read-only lookup).

We no longer create/realign/apply coupons; the resolver just answers whether a
Wave coupon code already exists in ERP. frappe.db.get_value is patched at the boundary.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.resolvers import coupon_resolver as cr


class TestFindCouponCode(FrappeTestCase):
	def test_returns_name_when_coupon_exists(self):
		with patch.object(frappe.db, "get_value", return_value="HYPA10") as mock_get:
			self.assertEqual(cr.find_coupon_code(" HYPA10 "), "HYPA10")
		mock_get.assert_called_once_with("Coupon Code", {"coupon_code": "HYPA10"}, "name")

	def test_returns_none_when_absent(self):
		with patch.object(frappe.db, "get_value", return_value=None):
			self.assertIsNone(cr.find_coupon_code("NOPE"))

	def test_blank_or_none_code_short_circuits_without_db(self):
		with patch.object(frappe.db, "get_value") as mock_get:
			self.assertIsNone(cr.find_coupon_code(""))
			self.assertIsNone(cr.find_coupon_code("   "))
			self.assertIsNone(cr.find_coupon_code(None))
		mock_get.assert_not_called()
