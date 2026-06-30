"""Unit tests for api.sales_invoice.ensure_payment_entry (the 'Wave Payment Entry' button, issue #193).

Pure-mock: the engine, frappe.get_doc, permission check, and commit are patched.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.api import sales_invoice as si_api

SI = "ACC-SINV-2026-00105"


def _doc(classification="prepaid"):
	doc = SimpleNamespace(doctype="Sales Invoice", name=SI)
	doc.check_permission = lambda perm: None
	doc.get = lambda key, default=None: {"wave_payment_classification": classification}.get(key, default)
	return doc


class TestEnsurePaymentEntry(FrappeTestCase):
	def test_not_prepaid_returns_engine_refusal(self):
		# The endpoint no longer pre-checks classification; the engine decides
		# authoritatively (from the source SO) and returns the not-prepaid refusal.
		with (
			patch.object(frappe, "get_doc", return_value=_doc(classification="cod")),
			patch.object(si_api.prepaid_pe_creator, "find_or_create_for_si",
				return_value={"ok": False, "reason": "Not a prepaid Wave order."}) as mock_engine,
			patch.object(frappe.db, "commit"),
		):
			result = si_api.ensure_payment_entry(SI)
		self.assertFalse(result["ok"])
		self.assertIn("correlation_id", result)
		mock_engine.assert_called_once()

	def test_prepaid_delegates_and_returns_envelope(self):
		envelope = {"ok": True, "created": True, "payment_entry": "ACC-PAY-X", "reason": "Payment Entry submitted."}
		doc = _doc()
		with (
			patch.object(frappe, "get_doc", return_value=doc),
			patch.object(si_api.prepaid_pe_creator, "find_or_create_for_si", return_value=dict(envelope)) as mock_engine,
			patch.object(frappe.db, "commit"),
		):
			result = si_api.ensure_payment_entry(SI)
		self.assertTrue(result["ok"])
		self.assertEqual(result["payment_entry"], "ACC-PAY-X")
		self.assertIn("correlation_id", result)
		mock_engine.assert_called_once()

	def test_enforces_submit_permission(self):
		doc = _doc()
		calls = []
		doc.check_permission = lambda perm: calls.append(perm)
		with (
			patch.object(frappe, "get_doc", return_value=doc),
			patch.object(si_api.prepaid_pe_creator, "find_or_create_for_si", return_value={"ok": True}),
			patch.object(frappe.db, "commit"),
		):
			si_api.ensure_payment_entry(SI)
		self.assertEqual(calls, ["submit"])
