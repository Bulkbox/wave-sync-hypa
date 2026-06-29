"""Unit tests for api.sales_order.verify_ipay_payment (issue #129).

The whitelisted button endpoint: prepaid guard, delegates to fetch_and_stamp,
returns the result with a correlation id.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.api import sales_order as so_api

SO = "SAL-ORD-2026-00105"


def _doc(classification="prepaid", docstatus=1):
	doc = SimpleNamespace(doctype="Sales Order", name=SO, docstatus=docstatus)
	doc.check_permission = lambda perm: None
	doc.get = lambda key, default=None: {"wave_payment_classification": classification}.get(key, default)
	return doc


class TestVerifyIpayPayment(FrappeTestCase):
	def setUp(self):
		# Pre-dates the prepaid-PE draft chain; neutralise its enqueue so these
		# tests don't load real Wave Settings.
		p = patch.object(so_api.prepaid_pe, "maybe_enqueue_draft_for_order")
		p.start()
		self.addCleanup(p.stop)

	def test_returns_ok_false_when_not_prepaid(self):
		with (
			patch.object(frappe, "get_doc", return_value=_doc(classification="cod")),
			patch.object(so_api.ipay_payment_sync, "fetch_and_stamp") as mock_fetch,
		):
			result = so_api.verify_ipay_payment(SO)
		self.assertFalse(result["ok"])
		mock_fetch.assert_not_called()
		# Early return still carries the full, uniform shape for the JS.
		self.assertIn("correlation_id", result)
		self.assertIn("paid", result)

	def test_prepaid_delegates_and_returns_result(self):
		fetched = {"ok": True, "paid": True, "data": {"transaction_code": "X"}, "reason": None}
		with (
			patch.object(frappe, "get_doc", return_value=_doc()),
			patch.object(so_api.ipay_payment_sync, "fetch_and_stamp", return_value=fetched) as mock_fetch,
			patch.object(frappe.db, "commit"),
		):
			result = so_api.verify_ipay_payment(SO)
		mock_fetch.assert_called_once()
		self.assertTrue(result["ok"])
		self.assertTrue(result["paid"])
		self.assertIn("correlation_id", result)
