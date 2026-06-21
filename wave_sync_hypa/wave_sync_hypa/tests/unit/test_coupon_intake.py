"""Unit tests for the coupon intake step in handlers.order_create.

Exercises _extract_valid_coupon and _apply_coupon in isolation; resolve_coupon
is patched at the module boundary so we pin the gating/guard/soft-fail wiring,
not the resolver itself (covered by test_coupon_resolver).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers import order_create as oc
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveResolutionError


def _settings(enabled: int = 1, divisor: int = 100) -> MagicMock:
	s = MagicMock(name="WaveSettings")
	s.price_scale_divisor = divisor
	s.get.side_effect = lambda key, default=None: {"coupon_sync_enabled": enabled}.get(key, default)
	return s


def _payload(coupon: dict | None = "default", subtotal_cents: int = 964000) -> dict:
	if coupon == "default":
		coupon = {"type": "COUPON", "value": "HYPA10", "amount": 1000, "isValid": True}
	return {"paymentOptions": [coupon] if coupon else [], "orderItemsPrice": subtotal_cents}


class TestExtractValidCoupon(FrappeTestCase):
	def test_returns_valid_coupon(self):
		self.assertEqual(oc._extract_valid_coupon(_payload())["value"], "HYPA10")

	def test_skips_invalid(self):
		self.assertIsNone(oc._extract_valid_coupon(_payload(
			{"type": "COUPON", "value": "X", "amount": 1000, "isValid": False})))

	def test_skips_zero_amount(self):
		self.assertIsNone(oc._extract_valid_coupon(_payload(
			{"type": "COUPON", "value": "X", "amount": 0, "isValid": True})))

	def test_ignores_non_coupon_option(self):
		self.assertIsNone(oc._extract_valid_coupon(_payload(
			{"type": "cash", "amount": 1000, "isValid": True})))

	def test_none_when_no_options(self):
		self.assertIsNone(oc._extract_valid_coupon(_payload(coupon=None)))


class TestApplyCoupon(FrappeTestCase):
	def test_disabled_flag_is_a_no_op(self):
		so = SimpleNamespace()
		with patch.object(oc, "resolve_coupon") as mock_resolve:
			skipped = oc._apply_coupon(so, _payload(), _settings(enabled=0), "c")
		self.assertEqual(skipped, [])
		mock_resolve.assert_not_called()
		self.assertFalse(hasattr(so, "coupon_code"))

	def test_applies_valid_coupon(self):
		so = SimpleNamespace()
		settings = _settings()
		with patch.object(oc, "resolve_coupon", return_value="HYPA10") as mock_resolve:
			skipped = oc._apply_coupon(so, _payload(), settings, "corr-1")
		self.assertEqual(skipped, [])
		self.assertEqual(so.coupon_code, "HYPA10")
		mock_resolve.assert_called_once_with("HYPA10", 10.0, settings, "corr-1")

	def test_amount_over_subtotal_is_skipped(self):
		so = SimpleNamespace()
		with patch.object(oc, "resolve_coupon") as mock_resolve:
			skipped = oc._apply_coupon(so, _payload(subtotal_cents=500), _settings(), "c")
		self.assertEqual(len(skipped), 1)
		self.assertIn("exceeds order subtotal", skipped[0]["error"])
		mock_resolve.assert_not_called()
		self.assertFalse(hasattr(so, "coupon_code"))

	def test_resolution_failure_is_soft_failed(self):
		so = SimpleNamespace()
		with patch.object(oc, "resolve_coupon", side_effect=WaveResolutionError("nope")):
			skipped = oc._apply_coupon(so, _payload(), _settings(), "c")
		self.assertEqual(len(skipped), 1)
		self.assertEqual(skipped[0]["code"], "HYPA10")
		self.assertFalse(hasattr(so, "coupon_code"))

	def test_no_coupon_in_payload_is_a_no_op(self):
		so = SimpleNamespace()
		with patch.object(oc, "resolve_coupon") as mock_resolve:
			skipped = oc._apply_coupon(so, _payload(coupon=None), _settings(), "c")
		self.assertEqual(skipped, [])
		mock_resolve.assert_not_called()
