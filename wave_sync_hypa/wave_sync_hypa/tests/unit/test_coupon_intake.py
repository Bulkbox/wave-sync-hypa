"""Unit tests for the coupon review step in handlers.order_create.

We never CREATE/UPDATE a coupon. An existing ERP coupon is APPLIED to the SO
(native coupon_code) inside a savepoint; a missing one is alarmed only. Either
way the order is flagged for team review. find_coupon_code + frappe.db savepoint
are patched at the module boundary.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers import order_create as oc

COUPON = {"type": "COUPON", "value": "HYPA10", "amount": 1000, "isValid": True}


class _FakeSO:
	"""Minimal persisted-SO stand-in: coupon_code slot + a save() that records calls."""

	def __init__(self):
		self.coupon_code = None
		self.saved = False

	def get(self, key, default=None):
		return getattr(self, key, default)

	def save(self, **kwargs):
		self.saved = True


def _settings(enabled: int = 1, divisor: int = 100) -> MagicMock:
	s = MagicMock(name="WaveSettings")
	s.price_scale_divisor = divisor
	s.get.side_effect = lambda key, default=None: {"coupon_sync_enabled": enabled}.get(key, default)
	return s


def _payload(*coupons: dict) -> dict:
	return {"paymentOptions": list(coupons)}


class TestExtractCoupons(FrappeTestCase):
	def test_returns_coupon_with_code(self):
		self.assertEqual([o["value"] for o in oc._extract_coupons(_payload(COUPON))], ["HYPA10"])

	def test_includes_invalid_and_zero_amount(self):
		# "Any coupon on the order" — Wave's isValid/amount must NOT filter it out.
		out = oc._extract_coupons(_payload(
			{"type": "COUPON", "value": "A", "amount": 0, "isValid": True},
			{"type": "COUPON", "value": "B", "amount": 500, "isValid": False},
		))
		self.assertEqual([o["value"] for o in out], ["A", "B"])

	def test_skips_codeless_and_non_coupon_options(self):
		out = oc._extract_coupons(_payload(
			{"type": "COUPON", "value": "  ", "amount": 100},
			{"type": "cash", "amount": 100},
		))
		self.assertEqual(out, [])

	def test_empty_when_no_options(self):
		self.assertEqual(oc._extract_coupons({}), [])


class TestReviewCoupons(FrappeTestCase):
	def test_disabled_flag_is_a_no_op(self):
		so = _FakeSO()
		with patch.object(oc, "find_coupon_code") as mock_find:
			reviews = oc._review_coupons(so, _payload(COUPON), _settings(enabled=0))
		self.assertEqual(reviews, [])
		mock_find.assert_not_called()
		self.assertFalse(so.saved)

	def test_no_coupon_is_a_no_op(self):
		so = _FakeSO()
		with patch.object(oc, "find_coupon_code") as mock_find:
			reviews = oc._review_coupons(so, _payload(), _settings())
		self.assertEqual(reviews, [])
		mock_find.assert_not_called()

	def test_found_coupon_is_applied_to_the_so(self):
		so = _FakeSO()
		with (
			patch.object(oc, "find_coupon_code", return_value="HYPA10"),
			patch.object(frappe.db, "savepoint"),
			patch.object(frappe.db, "rollback"),
		):
			reviews = oc._review_coupons(so, _payload(COUPON), _settings())
		self.assertEqual(reviews, [{"code": "HYPA10", "amount_major": 10.0, "found": True, "applied": True, "error": None}])
		self.assertEqual(so.coupon_code, "HYPA10")
		self.assertTrue(so.saved)

	def test_missing_coupon_is_alarmed_not_applied(self):
		so = _FakeSO()
		with patch.object(oc, "find_coupon_code", return_value=None):
			reviews = oc._review_coupons(so, _payload(COUPON), _settings())
		self.assertEqual(reviews[0]["found"], False)
		self.assertFalse(reviews[0]["applied"])
		self.assertIsNone(so.coupon_code)
		self.assertFalse(so.saved)

	def test_apply_failure_is_rolled_back_and_reported(self):
		so = _FakeSO()
		so.save = MagicMock(side_effect=Exception("discount exceeds total"))
		with (
			patch.object(oc, "find_coupon_code", return_value="HYPA10"),
			patch.object(frappe.db, "savepoint"),
			patch.object(frappe.db, "rollback") as mock_rollback,
		):
			reviews = oc._review_coupons(so, _payload(COUPON), _settings())
		self.assertTrue(reviews[0]["found"])
		self.assertFalse(reviews[0]["applied"])
		self.assertIn("discount exceeds total", reviews[0]["error"])
		self.assertIsNone(so.coupon_code)  # reset after rollback
		mock_rollback.assert_called_once_with(save_point="wave_coupon")

	def test_only_first_found_coupon_takes_the_single_slot(self):
		so = _FakeSO()
		with (
			patch.object(oc, "find_coupon_code", return_value="EXISTS"),
			patch.object(frappe.db, "savepoint"),
			patch.object(frappe.db, "rollback"),
		):
			reviews = oc._review_coupons(
				so,
				_payload({"type": "COUPON", "value": "A", "amount": 100},
						 {"type": "COUPON", "value": "B", "amount": 200}),
				_settings(),
			)
		self.assertTrue(reviews[0]["applied"])
		self.assertFalse(reviews[1]["applied"])  # slot already taken
		self.assertEqual(so.coupon_code, "EXISTS")

	def test_never_creates_a_coupon(self):
		"""Never create a Coupon Code / Pricing Rule — applying an existing one uses save(), not get_doc()."""
		so = _FakeSO()
		with (
			patch.object(oc, "find_coupon_code", return_value="HYPA10"),
			patch.object(frappe.db, "savepoint"),
			patch.object(frappe.db, "rollback"),
			patch.object(oc.frappe, "get_doc") as mock_get_doc,
		):
			oc._review_coupons(so, _payload(COUPON), _settings())
		mock_get_doc.assert_not_called()


class TestCouponReviewMessage(FrappeTestCase):
	def _msg(self, **over):
		entry = {"code": "HYPA10", "amount_major": 10.0, "found": True, "applied": True, "error": None}
		entry.update(over)
		return oc._coupon_review_message(entry)

	def test_applied_message(self):
		self.assertIn("applied to this order", self._msg(applied=True))

	def test_apply_failed_message(self):
		msg = self._msg(applied=False, error="discount exceeds total")
		self.assertIn("could not be applied", msg)
		self.assertIn("discount exceeds total", msg)

	def test_missing_message(self):
		self.assertIn("no matching ERP Coupon Code", self._msg(found=False, applied=False))

	def test_slot_taken_message(self):
		self.assertIn("another coupon is already on this order", self._msg(applied=False, error=None))
