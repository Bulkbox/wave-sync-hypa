"""Unit tests for services.ipay_gateway (issue #129).

The in-process bridge to the same-site iPay app. Confirms it never raises and
returns the right structured envelope across: app-not-installed, empty oid,
paid, not-paid, and iPay raising (misconfig / unreachable).
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.services import ipay_gateway

OID = "10000105"
PAID_DATA = {
	"oid": OID,
	"transaction_amount": "260.00",
	"transaction_code": "7799531096406646604275",
	"payment_mode": "VISA DEBIT",
	"paid_at": "2026-05-28 10:25:10",
	"firstname": "MARIANNA",
	"lastname": "CHRAPANA",
	"telephone": "0712345678",
}


class TestIpayGateway(FrappeTestCase):
	"""fetch_transaction: never raises; structured envelope per outcome."""

	def test_app_not_installed_returns_available_false(self):
		with patch.object(ipay_gateway, "is_ipay_available", return_value=False):
			result = ipay_gateway.fetch_transaction(OID)
		self.assertFalse(result["available"])
		self.assertFalse(result["paid"])
		self.assertIsNone(result["data"])

	def test_empty_oid_short_circuits(self):
		with patch.object(ipay_gateway, "is_ipay_available", return_value=True) as mock_avail:
			result = ipay_gateway.fetch_transaction("   ")
		self.assertFalse(result["paid"])
		self.assertEqual(result["error"], "oid is empty")
		mock_avail.assert_not_called()  # bailed before the install check

	def test_paid_returns_data(self):
		with (
			patch.object(ipay_gateway, "is_ipay_available", return_value=True),
			patch("ipay.api.get_transaction", return_value={"oid": OID, "paid": True, "data": PAID_DATA}),
		):
			result = ipay_gateway.fetch_transaction(OID)
		self.assertTrue(result["available"])
		self.assertTrue(result["paid"])
		self.assertEqual(result["data"]["transaction_code"], PAID_DATA["transaction_code"])
		self.assertIsNone(result["error"])

	def test_not_paid_returns_paid_false(self):
		with (
			patch.object(ipay_gateway, "is_ipay_available", return_value=True),
			patch("ipay.api.get_transaction", return_value={"oid": OID, "paid": False, "data": None}),
		):
			result = ipay_gateway.fetch_transaction(OID)
		self.assertFalse(result["paid"])
		self.assertIsNone(result["data"])
		self.assertIsNone(result["error"])

	def test_ipay_raises_is_swallowed_and_captured(self):
		"""iPay frappe.throw (unconfigured / unreachable) -> error captured, no raise."""
		with (
			patch.object(ipay_gateway, "is_ipay_available", return_value=True),
			patch("ipay.api.get_transaction", side_effect=frappe.ValidationError("iPay vendor id or API key is not configured.")),
		):
			result = ipay_gateway.fetch_transaction(OID)  # must not raise
		self.assertFalse(result["paid"])
		self.assertIsNone(result["data"])
		self.assertIn("not configured", result["error"])

	def test_installed_but_stale_app_gives_actionable_error(self):
		"""iPay installed but predating ipay.api -> clear 'update the iPay app' message, not a raw ImportError."""
		with (
			patch.object(ipay_gateway, "is_ipay_available", return_value=True),
			patch.dict("sys.modules", {"ipay.api": None}),
		):
			result = ipay_gateway.fetch_transaction(OID)  # must not raise
		self.assertTrue(result["available"])
		self.assertFalse(result["paid"])
		self.assertIsNone(result["data"])
		self.assertIn("update the iPay app", result["error"])
		self.assertNotIn("No module named", result["error"])
