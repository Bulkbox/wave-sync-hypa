"""Unit tests for the prepaid PE wiring (issue #193).

handlers.prepaid_pe (gate + enqueue glue), the Sales Invoice handler branch
(classification stamp + attach enqueue), and the Sales Order verify-success
chain. Pure-mock; the engine, master switch, and frappe boundary are patched.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.api import sales_order as so_api
from wave_sync_hypa.wave_sync_hypa.handlers import prepaid_pe as pe_handler
from wave_sync_hypa.wave_sync_hypa.handlers import sales_invoice as si_handler

SO = "SAL-ORD-2026-00105"
SI = "ACC-SINV-2026-00105"
WOID = "6a17ed37a7685d8ebbf3f9a6"


def _settings(auto=1):
	s = MagicMock(name="WaveSettings")
	s.get.side_effect = lambda key, default=None: {"ipay_auto_create_payment_entry": auto}.get(key, default)
	return s


class TestSoSubmitDraftEnqueue(FrappeTestCase):
	def _so(self, classification="prepaid"):
		doc = SimpleNamespace(name=SO, doctype="Sales Order")
		doc.get = lambda k, d=None: {"wave_payment_classification": classification}.get(k, d)
		return doc

	def test_prepaid_submit_enqueues_draft(self):
		with patch.object(pe_handler, "maybe_enqueue_draft_for_order") as mock_q:
			pe_handler.enqueue_draft_on_so_submit(self._so("prepaid"))
		mock_q.assert_called_once_with(SO)

	def test_non_prepaid_submit_does_nothing(self):
		with patch.object(pe_handler, "maybe_enqueue_draft_for_order") as mock_q:
			pe_handler.enqueue_draft_on_so_submit(self._so("cod"))
		mock_q.assert_not_called()


class TestMaybeEnqueueGates(FrappeTestCase):
	def test_draft_skipped_when_flag_off(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(auto=0)),
			patch.object(pe_handler.prepaid_pe_creator, "enqueue_draft_for_order") as mock_q,
		):
			pe_handler.maybe_enqueue_draft_for_order(SO)
		mock_q.assert_not_called()

	def test_draft_skipped_when_master_switch_off(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(auto=1)),
			patch.object(pe_handler, "skip_if_disabled", return_value=True),
			patch.object(pe_handler.prepaid_pe_creator, "enqueue_draft_for_order") as mock_q,
		):
			pe_handler.maybe_enqueue_draft_for_order(SO)
		mock_q.assert_not_called()

	def test_draft_enqueued_when_enabled(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(auto=1)),
			patch.object(pe_handler, "skip_if_disabled", return_value=False),
			patch.object(pe_handler.prepaid_pe_creator, "enqueue_draft_for_order") as mock_q,
		):
			pe_handler.maybe_enqueue_draft_for_order(SO)
		mock_q.assert_called_once()

	def test_attach_enqueued_when_enabled(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(auto=1)),
			patch.object(pe_handler, "skip_if_disabled", return_value=False),
			patch.object(pe_handler.prepaid_pe_creator, "enqueue_attach_for_si") as mock_q,
		):
			pe_handler.maybe_enqueue_attach_for_si(SI)
		mock_q.assert_called_once()

	def test_attach_skipped_when_flag_off(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(auto=0)),
			patch.object(pe_handler.prepaid_pe_creator, "enqueue_attach_for_si") as mock_q,
		):
			pe_handler.maybe_enqueue_attach_for_si(SI)
		mock_q.assert_not_called()


class TestSalesInvoiceWiring(FrappeTestCase):
	def test_stamp_sets_payment_classification(self):
		doc = MagicMock(name="SI")
		doc.get.side_effect = lambda k, d=None: {}.get(k, d)  # wave_order_id empty -> proceeds
		doc.doctype = "Sales Invoice"
		doc.name = SI

		with (
			patch.object(si_handler, "_collect_distinct_wave_order_ids", return_value=[WOID]),
			patch.object(
				frappe.db, "get_value",
				return_value=frappe._dict(wave_friendly_id="10000105", wave_payment_classification="prepaid"),
			),
		):
			si_handler.stamp_wave_order_id(doc)
		self.assertEqual(doc.wave_order_id, WOID)
		self.assertEqual(doc.wave_payment_classification, "prepaid")

	def test_regular_prepaid_submit_enqueues_attach(self):
		doc = MagicMock(name="SI")
		doc.get.side_effect = lambda k, d=None: {
			"is_return": 0, "wave_order_id": WOID, "wave_payment_classification": "prepaid"}.get(k, d)
		doc.name = SI
		with (
			patch.object(si_handler, "_collect_distinct_wave_order_ids", return_value=[WOID]),
			patch.object(si_handler.order_status, "dispatch_with_wave_order_ids"),
			patch.object(si_handler.prepaid_pe, "maybe_enqueue_attach_for_si") as mock_q,
		):
			si_handler.on_sales_invoice_submit(doc)
		mock_q.assert_called_once_with(SI)

	def test_cod_submit_does_not_enqueue_attach(self):
		doc = MagicMock(name="SI")
		doc.get.side_effect = lambda k, d=None: {
			"is_return": 0, "wave_order_id": WOID, "wave_payment_classification": "cod"}.get(k, d)
		doc.name = SI
		with (
			patch.object(si_handler, "_collect_distinct_wave_order_ids", return_value=[WOID]),
			patch.object(si_handler.order_status, "dispatch_with_wave_order_ids"),
			patch.object(si_handler.prepaid_pe, "maybe_enqueue_attach_for_si") as mock_q,
		):
			si_handler.on_sales_invoice_submit(doc)
		mock_q.assert_not_called()

	def test_return_invoice_does_not_enqueue_attach(self):
		doc = MagicMock(name="SI")
		doc.get.side_effect = lambda k, d=None: {"is_return": 1, "wave_order_id": WOID}.get(k, d)
		doc.name = SI
		with (
			patch.object(si_handler, "_collect_distinct_wave_order_ids", return_value=[WOID]),
			patch.object(si_handler, "_handle_return"),
			patch.object(si_handler.prepaid_pe, "maybe_enqueue_attach_for_si") as mock_q,
		):
			si_handler.on_sales_invoice_submit(doc)
		mock_q.assert_not_called()


class TestVerifyChainCreatesDraft(FrappeTestCase):
	def _doc(self, docstatus=1):
		doc = MagicMock(name="SO")
		doc.docstatus = docstatus
		doc.get.side_effect = lambda k, d=None: {"wave_payment_classification": "prepaid"}.get(k, d)
		doc.check_permission.return_value = None
		return doc

	def test_paid_on_submitted_order_enqueues_draft(self):
		with (
			patch.object(frappe, "get_doc", return_value=self._doc(docstatus=1)),
			patch.object(so_api.ipay_payment_sync, "fetch_and_stamp", return_value={"ok": True, "paid": True, "data": {}}),
			patch.object(so_api.prepaid_pe, "maybe_enqueue_draft_for_order") as mock_q,
			patch.object(frappe.db, "commit"),
		):
			so_api.verify_ipay_payment(SO)
		mock_q.assert_called_once_with(SO)

	def test_not_paid_does_not_enqueue(self):
		with (
			patch.object(frappe, "get_doc", return_value=self._doc(docstatus=1)),
			patch.object(so_api.ipay_payment_sync, "fetch_and_stamp", return_value={"ok": True, "paid": False, "data": None}),
			patch.object(so_api.prepaid_pe, "maybe_enqueue_draft_for_order") as mock_q,
			patch.object(frappe.db, "commit"),
		):
			so_api.verify_ipay_payment(SO)
		mock_q.assert_not_called()

	def test_paid_on_draft_order_does_not_enqueue(self):
		with (
			patch.object(frappe, "get_doc", return_value=self._doc(docstatus=0)),
			patch.object(so_api.ipay_payment_sync, "fetch_and_stamp", return_value={"ok": True, "paid": True, "data": {}}),
			patch.object(so_api.prepaid_pe, "maybe_enqueue_draft_for_order") as mock_q,
			patch.object(frappe.db, "commit"),
		):
			so_api.verify_ipay_payment(SO)
		mock_q.assert_not_called()
