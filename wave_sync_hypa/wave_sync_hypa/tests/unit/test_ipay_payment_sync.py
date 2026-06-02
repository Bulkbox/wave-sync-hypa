"""Unit tests for services.ipay_payment_sync (issue #129).

fetch_and_stamp core: prepaid guard, paid -> stamp + clear flag, unverified ->
stamp paid=0 + raise flag. Plus the async worker's master-switch / iPay-flag
backstops. gateway + review_flag + db writes are patched at the module
boundary so no real DocType is touched.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.services import ipay_payment_sync

SO = "SAL-ORD-2026-00105"
OID = "10000105"
PAID_DATA = {
	"transaction_amount": "260.00",
	"transaction_code": "7799531096406646604275",
	"payment_mode": "VISA DEBIT",
	"paid_at": "2026-05-28 10:25:10",
	"firstname": "MARIANNA",
	"lastname": "CHRAPANA",
	"telephone": "0712345678",
}


def _so_row(classification="prepaid", friendly=OID):
	# frappe.db.get_value(..., as_dict=True) returns a frappe._dict (attribute access).
	return frappe._dict(name=SO, wave_payment_classification=classification, wave_friendly_id=friendly)


def _settings(ipay_enabled=1):
	s = MagicMock(name="WaveSettings")
	s.get.side_effect = lambda key, default=None: {"ipay_verification_enabled": ipay_enabled}.get(key, default)
	return s


class TestFetchAndStamp(FrappeTestCase):
	"""The shared core: enforces both gates, then verifies + stamps + flags/clears."""

	def test_master_switch_off_returns_disabled_without_lookup(self):
		with (
			patch.object(ipay_payment_sync, "is_wave_integration_enabled", return_value=False),
			patch.object(frappe.db, "get_value") as mock_get,
			patch.object(ipay_payment_sync.ipay_gateway, "fetch_transaction") as mock_fetch,
		):
			result = ipay_payment_sync.fetch_and_stamp(SO, "corr-1", settings=_settings())
		self.assertFalse(result["ok"])
		mock_get.assert_not_called()
		mock_fetch.assert_not_called()

	def test_ipay_flag_off_returns_disabled_without_lookup(self):
		with (
			patch.object(ipay_payment_sync, "is_wave_integration_enabled", return_value=True),
			patch.object(frappe.db, "get_value") as mock_get,
			patch.object(ipay_payment_sync.ipay_gateway, "fetch_transaction") as mock_fetch,
		):
			result = ipay_payment_sync.fetch_and_stamp(SO, "corr-1", settings=_settings(ipay_enabled=0))
		self.assertFalse(result["ok"])
		mock_get.assert_not_called()
		mock_fetch.assert_not_called()

	def test_skips_when_not_prepaid(self):
		with (
			patch.object(ipay_payment_sync, "is_wave_integration_enabled", return_value=True),
			patch.object(frappe.db, "get_value", return_value=_so_row(classification="cod")),
			patch.object(ipay_payment_sync.ipay_gateway, "fetch_transaction") as mock_fetch,
			patch.object(ipay_payment_sync, "log_step"),
		):
			result = ipay_payment_sync.fetch_and_stamp(SO, "corr-1", settings=_settings())
		self.assertFalse(result["ok"])
		mock_fetch.assert_not_called()

	def test_no_friendly_id_flags_for_review(self):
		with (
			patch.object(ipay_payment_sync, "is_wave_integration_enabled", return_value=True),
			patch.object(frappe.db, "get_value", return_value=_so_row(friendly="")),
			patch.object(ipay_payment_sync.payment_review_flag, "flag") as mock_flag,
			patch.object(ipay_payment_sync.ipay_gateway, "fetch_transaction") as mock_fetch,
			patch.object(ipay_payment_sync, "log_step"),
		):
			result = ipay_payment_sync.fetch_and_stamp(SO, "corr-1", settings=_settings())
		self.assertFalse(result["ok"])
		mock_fetch.assert_not_called()
		mock_flag.assert_called_once()

	def test_paid_stamps_fields_and_clears_flag(self):
		with (
			patch.object(ipay_payment_sync, "is_wave_integration_enabled", return_value=True),
			patch.object(frappe.db, "get_value", return_value=_so_row()),
			patch.object(frappe.db, "set_value") as mock_set,
			patch.object(
				ipay_payment_sync.ipay_gateway, "fetch_transaction",
				return_value={"available": True, "paid": True, "data": PAID_DATA, "error": None},
			),
			patch.object(ipay_payment_sync.payment_review_flag, "clear") as mock_clear,
			patch.object(ipay_payment_sync.payment_review_flag, "flag") as mock_flag,
			patch.object(ipay_payment_sync, "log_step"),
		):
			result = ipay_payment_sync.fetch_and_stamp(SO, "corr-1", settings=_settings())

		self.assertTrue(result["ok"])
		self.assertTrue(result["paid"])
		mock_clear.assert_called_once()
		mock_flag.assert_not_called()
		# Stamped the confirmed fields on the SO.
		stamped = mock_set.call_args.args[2]
		self.assertEqual(stamped["wave_ipay_paid"], 1)
		self.assertEqual(stamped["wave_ipay_transaction_code"], PAID_DATA["transaction_code"])
		self.assertEqual(stamped["wave_ipay_payment_mode"], "VISA DEBIT")
		self.assertEqual(stamped["wave_ipay_payer_name"], "MARIANNA CHRAPANA")

	def test_unverified_stamps_not_paid_and_flags(self):
		with (
			patch.object(ipay_payment_sync, "is_wave_integration_enabled", return_value=True),
			patch.object(frappe.db, "get_value", return_value=_so_row()),
			patch.object(frappe.db, "set_value") as mock_set,
			patch.object(
				ipay_payment_sync.ipay_gateway, "fetch_transaction",
				return_value={"available": True, "paid": False, "data": None, "error": None},
			),
			patch.object(ipay_payment_sync.payment_review_flag, "clear") as mock_clear,
			patch.object(ipay_payment_sync.payment_review_flag, "flag") as mock_flag,
			patch.object(ipay_payment_sync, "log_step"),
		):
			result = ipay_payment_sync.fetch_and_stamp(SO, "corr-1", settings=_settings())

		self.assertTrue(result["ok"])
		self.assertFalse(result["paid"])
		mock_flag.assert_called_once()
		mock_clear.assert_not_called()
		self.assertEqual(mock_set.call_args.args[2]["wave_ipay_paid"], 0)


class TestFetchAndStampWorker(FrappeTestCase):
	"""Worker: delegates to the (gate-enforcing) core; never raises."""

	def test_delegates_to_core(self):
		with patch.object(ipay_payment_sync, "fetch_and_stamp") as mock_core:
			ipay_payment_sync.fetch_and_stamp_worker(sales_order_name=SO, correlation_id="c")
		mock_core.assert_called_once_with(SO, "c")

	def test_unexpected_error_is_swallowed(self):
		with (
			patch.object(ipay_payment_sync, "fetch_and_stamp", side_effect=RuntimeError("boom")),
			patch.object(ipay_payment_sync, "log_step") as mock_log,
		):
			ipay_payment_sync.fetch_and_stamp_worker(sales_order_name=SO, correlation_id="c")  # no raise
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(ipay_payment_sync.STEP_UNEXPECTED_ERROR, steps)
