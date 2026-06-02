"""Unit tests for services.prepaid_pe_creator (issue #131).

Covers the find-update-attach / create branches plus the worker gates. The
ERPNext get_payment_entry helper, frappe.get_doc, db reads, and the
payment_review_flag service are patched at the module boundary.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers import sales_invoice as si_handler
from wave_sync_hypa.wave_sync_hypa.services import prepaid_pe_creator as pe_creator

SI = "ACC-SINV-2026-00105"
SO = "SAL-ORD-2026-00105"
TXN = "7799531096406646604275"


def _si_row(docstatus=1, is_return=0, customer="Cust", outstanding=260.0):
	return frappe._dict(
		docstatus=docstatus, is_return=is_return, customer=customer, outstanding_amount=outstanding
	)


def _settings(auto=1):
	s = MagicMock(name="WaveSettings")
	s.get.side_effect = lambda key, default=None: {
		"ipay_auto_create_payment_entry": auto,
		"payment_method_mappings": [],
	}.get(key, default)
	return s


def _prepaid_source():
	return [{"so": SO, "transaction_code": TXN, "paid_at": "2026-05-28 10:25:10", "payment_type": "card"}]


class TestEnsurePaymentEntry(FrappeTestCase):
	"""find-update-attach / create, deduped by reference_no == transaction code."""

	def test_skips_non_prepaid_invoice(self):
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row()),
			patch.object(pe_creator, "_prepaid_sources", return_value=[]),
			patch.object(pe_creator, "log_step") as mock_log,
		):
			pe_creator._ensure_payment_entry(SI, _settings(), "c")
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pe_creator.STEP_SKIPPED_NOT_PREPAID, steps)

	def test_multi_source_flags_si(self):
		two = _prepaid_source() + [{"so": "SO-2", "transaction_code": "T2", "paid_at": None, "payment_type": "card"}]
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row()),
			patch.object(pe_creator, "_prepaid_sources", return_value=two),
			patch.object(pe_creator.payment_review_flag, "flag") as mock_flag,
			patch.object(pe_creator, "log_step"),
		):
			pe_creator._ensure_payment_entry(SI, _settings(), "c")
		mock_flag.assert_called_once()

	def test_no_transaction_code_flags_si(self):
		no_txn = [{"so": SO, "transaction_code": "", "paid_at": None, "payment_type": "card"}]
		with (
			patch.object(frappe.db, "get_value", side_effect=[_si_row(), ""]),
			patch.object(pe_creator, "_prepaid_sources", return_value=no_txn),
			patch.object(pe_creator.ipay_payment_sync, "fetch_and_stamp"),
			patch.object(pe_creator.payment_review_flag, "flag") as mock_flag,
			patch.object(pe_creator, "log_step") as mock_log,
		):
			pe_creator._ensure_payment_entry(SI, _settings(), "c")
		mock_flag.assert_called_once()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pe_creator.STEP_NO_TXN_CODE, steps)

	def test_creates_new_pe_when_none_exists(self):
		fake_pe = MagicMock(name="PE")
		fake_pe.name = "ACC-PAY-0001"
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row()),
			patch.object(pe_creator, "_prepaid_sources", return_value=_prepaid_source()),
			patch.object(pe_creator, "_find_pe_by_reference", return_value=None),
			patch.object(pe_creator, "get_payment_entry", return_value=fake_pe) as mock_get_pe,
			patch.object(pe_creator.payment_review_flag, "clear") as mock_clear,
			patch.object(pe_creator, "log_step") as mock_log,
		):
			pe_creator._ensure_payment_entry(SI, _settings(), "c")

		mock_get_pe.assert_called_once_with("Sales Invoice", SI, reference_date=pe_creator._reference_date(_prepaid_source()[0]))
		self.assertEqual(fake_pe.reference_no, TXN)
		fake_pe.insert.assert_called_once()
		fake_pe.submit.assert_called_once()
		mock_clear.assert_called_once()  # success clears any prior SI flag
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pe_creator.STEP_CREATED, steps)
		self.assertIn(pe_creator.STEP_SUBMITTED, steps)

	def test_updates_existing_draft_and_attaches_si(self):
		draft = MagicMock(name="DraftPE")
		draft.name = "ACC-PAY-DRAFT"
		draft.references = []
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row()),
			patch.object(pe_creator, "_prepaid_sources", return_value=_prepaid_source()),
			patch.object(pe_creator, "_find_pe_by_reference", return_value=("ACC-PAY-DRAFT", 0)),
			patch.object(frappe, "get_doc", return_value=draft),
			patch.object(pe_creator.payment_review_flag, "clear"),
			patch.object(pe_creator, "log_step") as mock_log,
		):
			pe_creator._ensure_payment_entry(SI, _settings(), "c")

		# Our data wins: party reassigned to the SI customer, SI attached, submitted.
		self.assertEqual(draft.party, "Cust")
		self.assertEqual(draft.party_type, "Customer")
		draft.append.assert_called_once()
		self.assertEqual(draft.append.call_args.args[0], "references")
		draft.save.assert_called_once()
		draft.submit.assert_called_once()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pe_creator.STEP_UPDATED_DRAFT, steps)

	def test_submitted_pe_already_referencing_si_is_idempotent(self):
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row()),
			patch.object(pe_creator, "_prepaid_sources", return_value=_prepaid_source()),
			patch.object(pe_creator, "_find_pe_by_reference", return_value=("ACC-PAY-SUB", 1)),
			patch.object(pe_creator, "_pe_references_si", return_value=True),
			patch.object(pe_creator.payment_review_flag, "flag") as mock_flag,
			patch.object(pe_creator, "log_step") as mock_log,
		):
			pe_creator._ensure_payment_entry(SI, _settings(), "c")
		mock_flag.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pe_creator.STEP_ALREADY_SETTLED, steps)

	def test_submitted_pe_not_referencing_si_flags(self):
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row()),
			patch.object(pe_creator, "_prepaid_sources", return_value=_prepaid_source()),
			patch.object(pe_creator, "_find_pe_by_reference", return_value=("ACC-PAY-SUB", 1)),
			patch.object(pe_creator, "_pe_references_si", return_value=False),
			patch.object(pe_creator.payment_review_flag, "flag") as mock_flag,
			patch.object(pe_creator, "log_step") as mock_log,
		):
			pe_creator._ensure_payment_entry(SI, _settings(), "c")
		mock_flag.assert_called_once()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pe_creator.STEP_BLOCKED_SUBMITTED_PE, steps)

	def test_submit_block_leaves_draft_and_flags(self):
		fake_pe = MagicMock(name="PE")
		fake_pe.name = "ACC-PAY-0002"
		fake_pe.submit.side_effect = frappe.ValidationError("amount mismatch")
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row()),
			patch.object(pe_creator, "_prepaid_sources", return_value=_prepaid_source()),
			patch.object(pe_creator, "_find_pe_by_reference", return_value=None),
			patch.object(pe_creator, "get_payment_entry", return_value=fake_pe),
			patch.object(pe_creator.payment_review_flag, "flag") as mock_flag,
			patch.object(pe_creator.payment_review_flag, "clear") as mock_clear,
			patch.object(pe_creator, "log_step") as mock_log,
		):
			pe_creator._ensure_payment_entry(SI, _settings(), "c")
		fake_pe.insert.assert_called_once()
		mock_flag.assert_called_once()  # SI flagged for accounting
		mock_clear.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pe_creator.STEP_SUBMIT_BLOCKED, steps)


class TestWorkerGates(FrappeTestCase):
	def test_master_switch_off_skips(self):
		with (
			patch.object(pe_creator, "is_wave_integration_enabled", return_value=False),
			patch.object(pe_creator, "_ensure_payment_entry") as mock_core,
		):
			pe_creator.create_payment_entry_worker(sales_invoice=SI, correlation_id="c")
		mock_core.assert_not_called()

	def test_auto_create_flag_off_skips(self):
		with (
			patch.object(pe_creator, "is_wave_integration_enabled", return_value=True),
			patch.object(frappe, "get_cached_doc", return_value=_settings(auto=0)),
			patch.object(pe_creator, "_ensure_payment_entry") as mock_core,
		):
			pe_creator.create_payment_entry_worker(sales_invoice=SI, correlation_id="c")
		mock_core.assert_not_called()

	def test_enabled_calls_core(self):
		with (
			patch.object(pe_creator, "is_wave_integration_enabled", return_value=True),
			patch.object(frappe, "get_cached_doc", return_value=_settings(auto=1)),
			patch.object(pe_creator, "_ensure_payment_entry") as mock_core,
		):
			pe_creator.create_payment_entry_worker(sales_invoice=SI, correlation_id="c")
		mock_core.assert_called_once()

	def test_unexpected_error_is_swallowed(self):
		with (
			patch.object(pe_creator, "is_wave_integration_enabled", side_effect=RuntimeError("boom")),
			patch.object(pe_creator, "log_step") as mock_log,
		):
			pe_creator.create_payment_entry_worker(sales_invoice=SI, correlation_id="c")  # no raise
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pe_creator.STEP_UNEXPECTED_ERROR, steps)


class TestSalesInvoiceHandlerBranch(FrappeTestCase):
	"""on_sales_invoice_submit's prepaid branch: enqueue gated by flag + master switch."""

	def _doc(self):
		doc = SimpleNamespace(doctype="Sales Invoice", name=SI)
		doc.get = lambda key, default=None: {}.get(key, default)
		return doc

	def test_enqueues_when_flag_on(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(auto=1)),
			patch.object(si_handler, "skip_if_disabled", return_value=False),
			patch.object(si_handler.prepaid_pe_creator, "enqueue_payment_entry_creation") as mock_enq,
		):
			si_handler._maybe_create_prepaid_payment_entry(self._doc())
		mock_enq.assert_called_once()

	def test_no_enqueue_when_flag_off(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(auto=0)),
			patch.object(si_handler.prepaid_pe_creator, "enqueue_payment_entry_creation") as mock_enq,
		):
			si_handler._maybe_create_prepaid_payment_entry(self._doc())
		mock_enq.assert_not_called()

	def test_no_enqueue_when_master_switch_off(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(auto=1)),
			patch.object(si_handler, "skip_if_disabled", return_value=True),
			patch.object(si_handler.prepaid_pe_creator, "enqueue_payment_entry_creation") as mock_enq,
		):
			si_handler._maybe_create_prepaid_payment_entry(self._doc())
		mock_enq.assert_not_called()
