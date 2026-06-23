"""Unit tests for the coupon review step in handlers.order_create.

Exercises _extract_coupons, _review_coupons and the review message. We never
apply coupons — every couponed order is flagged for team review with a
found / not-found message. find_coupon_code is patched at the module boundary.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers import order_create as oc

COUPON = {"type": "COUPON", "value": "HYPA10", "amount": 1000, "isValid": True}


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
		with patch.object(oc, "find_coupon_code") as mock_find:
			reviews = oc._review_coupons(_payload(COUPON), _settings(enabled=0))
		self.assertEqual(reviews, [])
		mock_find.assert_not_called()

	def test_no_coupon_is_a_no_op(self):
		with patch.object(oc, "find_coupon_code") as mock_find:
			reviews = oc._review_coupons(_payload(), _settings())
		self.assertEqual(reviews, [])
		mock_find.assert_not_called()

	def test_found_coupon_marks_found_true(self):
		with patch.object(oc, "find_coupon_code", return_value="HYPA10"):
			reviews = oc._review_coupons(_payload(COUPON), _settings())
		self.assertEqual(reviews, [{"code": "HYPA10", "amount_major": 10.0, "found": True}])

	def test_missing_coupon_marks_found_false(self):
		with patch.object(oc, "find_coupon_code", return_value=None):
			reviews = oc._review_coupons(_payload(COUPON), _settings())
		self.assertEqual(reviews[0]["found"], False)

	def test_never_creates_or_applies_a_coupon(self):
		"""Alarm-only: reviewing must never create a Coupon Code / Pricing Rule (no get_doc)."""
		with (
			patch.object(oc, "find_coupon_code", return_value=None),
			patch.object(oc.frappe, "get_doc") as mock_get_doc,
		):
			oc._review_coupons(_payload(COUPON), _settings())
		mock_get_doc.assert_not_called()


class TestCouponReviewMessage(FrappeTestCase):
	def test_found_message_asks_to_confirm(self):
		msg = oc._coupon_review_message({"code": "HYPA10", "amount_major": 10.0, "found": True})
		self.assertIn("exists in ERP", msg)
		self.assertIn("HYPA10", msg)

	def test_missing_message_flags_absence(self):
		msg = oc._coupon_review_message({"code": "HYPA10", "amount_major": 10.0, "found": False})
		self.assertIn("no matching ERP Coupon Code", msg)
