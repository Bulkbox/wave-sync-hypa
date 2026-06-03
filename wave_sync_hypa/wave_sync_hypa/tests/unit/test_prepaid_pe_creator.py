"""Unit tests for services.prepaid_pe_creator (issues #131, hardening from the critical review).

Covers find-update-attach / create, the per-transaction lock + n8n duplicate
detection, the submitted-PE conflict alarm, the amount-reconciliation gate
(create-but-don't-submit on mismatch), multi-source flagging, and the worker
gates. ERPNext get_payment_entry, frappe.get_doc, db reads, the filelock, and
the payment_review_flag/alarm helpers are patched at the module boundary.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils.file_lock import LockTimeoutError

from wave_sync_hypa.wave_sync_hypa.handlers import sales_invoice as si_handler
from wave_sync_hypa.wave_sync_hypa.services import prepaid_pe_creator as pe_creator

SI = "ACC-SINV-2026-00105"
SO = "SAL-ORD-2026-00105"
TXN = "7799531096406646604275"


def _si_row(docstatus=1, is_return=0, customer="Cust", outstanding=260.0, grand_total=260.0, owner="ops@example.com"):
	return frappe._dict(
		docstatus=docstatus, is_return=is_return, customer=customer,
		outstanding_amount=outstanding, grand_total=grand_total, owner=owner,
	)


def _settings(auto=1):
	s = MagicMock(name="WaveSettings")
	s.get.side_effect = lambda key, default=None: {
		"ipay_auto_create_payment_entry": auto,
		"payment_method_mappings": [],
		"wave_payment_review_assignee": "accountant@example.com",
	}.get(key, default)
	return s


def _prepaid_source(hold=260.0, additional=0.0):
	return [{
		"so": SO, "transaction_code": TXN, "paid_at": "2026-05-28 10:25:10",
		"payment_type": "card", "wave_payment_hold": hold, "wave_additional_payment_hold": additional,
	}]


@contextlib.contextmanager
def _nolock(*a, **k):
	yield


class TestEnsurePaymentEntry(FrappeTestCase):
	"""find-update-attach / create, deduped by reference_no == transaction code; lock patched out."""

	def setUp(self):
		# The per-transaction filelock touches the filesystem; stub it in unit tests.
		self._lock_patch = patch.object(pe_creator, "filelock", _nolock)
		self._lock_patch.start()
		self.addCleanup(self._lock_patch.stop)

	def test_skips_non_prepaid_invoice(self):
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row()),
			patch.object(pe_creator, "_prepaid_sources", return_value=[]),
			patch.object(pe_creator, "log_step") as mock_log,
		):
			pe_creator._ensure_payment_entry(SI, _settings(), "c")
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pe_creator.STEP_SKIPPED_NOT_PREPAID, steps)

	def test_multi_source_flags_with_source_list(self):
		two = _prepaid_source() + [{"so": "SO-2", "transaction_code": "T2", "paid_at": None,
			"payment_type": "card", "wave_payment_hold": 0, "wave_additional_payment_hold": 0}]
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row()),
			patch.object(pe_creator, "_prepaid_sources", return_value=two),
			patch.object(pe_creator.payment_review_flag, "flag") as mock_flag,
			patch.object(pe_creator, "log_step") as mock_log,
		):
			pe_creator._ensure_payment_entry(SI, _settings(), "c")
		mock_flag.assert_called_once()
		multi = [c for c in mock_log.call_args_list if c.kwargs.get("step") == pe_creator.STEP_MULTI_SOURCE]
		self.assertEqual(multi[0].kwargs["request_body"]["sales_orders"], [SO, "SO-2"])

	def test_no_transaction_code_flags_si(self):
		no_txn = [{"so": SO, "transaction_code": "", "paid_at": None, "payment_type": "card",
			"wave_payment_hold": 0, "wave_additional_payment_hold": 0}]
		with (
			patch.object(frappe.db, "get_value", side_effect=[_si_row(), ""]),
			patch.object(pe_creator, "_prepaid_sources", return_value=no_txn),
			patch.object(pe_creator.ipay_payment_sync, "fetch_and_stamp"),
			patch.object(pe_creator.payment_review_flag, "flag") as mock_flag,
			patch.object(pe_creator, "log_step") as mock_log,
		):
			pe_creator._ensure_payment_entry(SI, _settings(), "c")
		mock_flag.assert_called_once()
		self.assertIn(pe_creator.STEP_NO_TXN_CODE, [c.kwargs.get("step") for c in mock_log.call_args_list])

	def test_lock_timeout_flags_si(self):
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row()),
			patch.object(pe_creator, "_prepaid_sources", return_value=_prepaid_source()),
			patch.object(pe_creator, "filelock", side_effect=LockTimeoutError("busy")),
			patch.object(pe_creator.payment_review_flag, "flag") as mock_flag,
			patch.object(pe_creator, "log_step") as mock_log,
		):
			pe_creator._ensure_payment_entry(SI, _settings(), "c")
		mock_flag.assert_called_once()
		self.assertIn(pe_creator.STEP_LOCK_TIMEOUT, [c.kwargs.get("step") for c in mock_log.call_args_list])

	def test_creates_and_submits_when_amount_reconciles(self):
		fake_pe = MagicMock(name="PE")
		fake_pe.name = "ACC-PAY-0001"
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row(grand_total=260.0)),
			patch.object(pe_creator, "_prepaid_sources", return_value=_prepaid_source(hold=260.0)),
			patch.object(pe_creator, "_find_pe_by_reference", return_value=None),
			patch.object(pe_creator, "_other_live_pes_by_reference", return_value=[]),
			patch.object(pe_creator, "get_payment_entry", return_value=fake_pe),
			patch.object(pe_creator.payment_review_flag, "clear") as mock_clear,
			patch.object(pe_creator, "log_step") as mock_log,
		):
			pe_creator._ensure_payment_entry(SI, _settings(), "c")
		self.assertEqual(fake_pe.reference_no, TXN)
		fake_pe.insert.assert_called_once()
		fake_pe.submit.assert_called_once()
		mock_clear.assert_called_once()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pe_creator.STEP_CREATED, steps)
		self.assertIn(pe_creator.STEP_SUBMITTED, steps)

	def test_amount_mismatch_creates_draft_but_does_not_submit(self):
		fake_pe = MagicMock(name="PE")
		fake_pe.name = "ACC-PAY-0009"
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row(grand_total=250.0)),
			patch.object(pe_creator, "_prepaid_sources", return_value=_prepaid_source(hold=260.0)),
			patch.object(pe_creator, "_find_pe_by_reference", return_value=None),
			patch.object(pe_creator, "_other_live_pes_by_reference", return_value=[]),
			patch.object(pe_creator, "get_payment_entry", return_value=fake_pe),
			patch.object(pe_creator.payment_review_flag, "flag") as mock_flag,
			patch.object(pe_creator, "log_step") as mock_log,
		):
			pe_creator._ensure_payment_entry(SI, _settings(), "c")
		fake_pe.insert.assert_called_once()       # PE created (draft)
		fake_pe.submit.assert_not_called()        # but NOT submitted
		mock_flag.assert_called_once()            # SI flagged
		self.assertIn(pe_creator.STEP_AMOUNT_MISMATCH, [c.kwargs.get("step") for c in mock_log.call_args_list])

	def test_missing_hold_creates_draft_but_does_not_submit(self):
		"""No Wave-authorised hold to verify against -> draft, no submit, flag (can't verify)."""
		fake_pe = MagicMock(name="PE")
		fake_pe.name = "ACC-PAY-NOHOLD"
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row(grand_total=260.0)),
			patch.object(pe_creator, "_prepaid_sources", return_value=_prepaid_source(hold=0.0)),
			patch.object(pe_creator, "_find_pe_by_reference", return_value=None),
			patch.object(pe_creator, "_other_live_pes_by_reference", return_value=[]),
			patch.object(pe_creator, "get_payment_entry", return_value=fake_pe),
			patch.object(pe_creator.payment_review_flag, "flag") as mock_flag,
			patch.object(pe_creator, "log_step") as mock_log,
		):
			pe_creator._ensure_payment_entry(SI, _settings(), "c")
		fake_pe.insert.assert_called_once()
		fake_pe.submit.assert_not_called()
		mock_flag.assert_called_once()
		self.assertIn(pe_creator.STEP_AMOUNT_MISMATCH, [c.kwargs.get("step") for c in mock_log.call_args_list])

	def test_concurrent_duplicate_detected_leaves_drafts_and_alarms(self):
		fake_pe = MagicMock(name="PE")
		fake_pe.name = "ACC-PAY-OURS"
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row()),
			patch.object(pe_creator, "_prepaid_sources", return_value=_prepaid_source()),
			patch.object(pe_creator, "_find_pe_by_reference", return_value=None),
			patch.object(pe_creator, "_other_live_pes_by_reference", return_value=["ACC-PAY-N8N"]),
			patch.object(pe_creator, "get_payment_entry", return_value=fake_pe),
			patch.object(pe_creator, "_comment_on_pe") as mock_comment,
			patch.object(pe_creator, "_assign_pe"),
			patch.object(pe_creator, "_notify_si_owner") as mock_notify,
			patch.object(pe_creator.payment_review_flag, "flag") as mock_flag,
			patch.object(pe_creator, "log_step") as mock_log,
		):
			pe_creator._ensure_payment_entry(SI, _settings(), "c")
		fake_pe.submit.assert_not_called()
		mock_flag.assert_called_once()
		mock_notify.assert_called_once()
		# both PEs get the conflict comment
		self.assertEqual({c.args[0] for c in mock_comment.call_args_list}, {"ACC-PAY-OURS", "ACC-PAY-N8N"})
		self.assertIn(pe_creator.STEP_DUPLICATE_DETECTED, [c.kwargs.get("step") for c in mock_log.call_args_list])

	def test_updates_existing_draft_and_attaches_si(self):
		draft = MagicMock(name="DraftPE")
		draft.name = "ACC-PAY-DRAFT"
		draft.references = []
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row(grand_total=260.0)),
			patch.object(pe_creator, "_prepaid_sources", return_value=_prepaid_source(hold=260.0)),
			patch.object(pe_creator, "_find_pe_by_reference", return_value=("ACC-PAY-DRAFT", 0)),
			patch.object(frappe, "get_doc", return_value=draft),
			patch.object(pe_creator.payment_review_flag, "clear"),
			patch.object(pe_creator, "log_step") as mock_log,
		):
			pe_creator._ensure_payment_entry(SI, _settings(), "c")
		self.assertEqual(draft.party, "Cust")
		self.assertEqual(draft.party_type, "Customer")
		draft.append.assert_called_once()
		self.assertEqual(draft.append.call_args.args[0], "references")
		draft.save.assert_called_once()
		draft.submit.assert_called_once()
		self.assertIn(pe_creator.STEP_UPDATED_DRAFT, [c.kwargs.get("step") for c in mock_log.call_args_list])

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
		self.assertIn(pe_creator.STEP_ALREADY_SETTLED, [c.kwargs.get("step") for c in mock_log.call_args_list])

	def test_submitted_pe_conflict_raises_full_alarm(self):
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row()),
			patch.object(pe_creator, "_prepaid_sources", return_value=_prepaid_source()),
			patch.object(pe_creator, "_find_pe_by_reference", return_value=("ACC-PAY-SUB", 1)),
			patch.object(pe_creator, "_pe_references_si", return_value=False),
			patch.object(pe_creator, "_comment_on_pe") as mock_comment,
			patch.object(pe_creator, "_assign_pe") as mock_assign,
			patch.object(pe_creator, "_notify_si_owner") as mock_notify,
			patch.object(pe_creator.payment_review_flag, "flag") as mock_flag,
			patch.object(pe_creator, "log_step") as mock_log,
		):
			pe_creator._ensure_payment_entry(SI, _settings(), "c")
		# never touches the submitted PE; alarms on every channel
		mock_flag.assert_called_once()
		mock_comment.assert_called_once()
		self.assertEqual(mock_comment.call_args.args[0], "ACC-PAY-SUB")
		mock_assign.assert_called_once()
		mock_notify.assert_called_once()
		self.assertIn(pe_creator.STEP_BLOCKED_SUBMITTED_PE, [c.kwargs.get("step") for c in mock_log.call_args_list])

	def test_submit_block_leaves_draft_and_flags(self):
		fake_pe = MagicMock(name="PE")
		fake_pe.name = "ACC-PAY-0002"
		fake_pe.submit.side_effect = frappe.ValidationError("validator block")
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row(grand_total=260.0)),
			patch.object(pe_creator, "_prepaid_sources", return_value=_prepaid_source(hold=260.0)),
			patch.object(pe_creator, "_find_pe_by_reference", return_value=None),
			patch.object(pe_creator, "_other_live_pes_by_reference", return_value=[]),
			patch.object(pe_creator, "get_payment_entry", return_value=fake_pe),
			patch.object(pe_creator.payment_review_flag, "flag") as mock_flag,
			patch.object(pe_creator, "log_step") as mock_log,
		):
			pe_creator._ensure_payment_entry(SI, _settings(), "c")
		fake_pe.insert.assert_called_once()
		mock_flag.assert_called_once()
		self.assertIn(pe_creator.STEP_SUBMIT_BLOCKED, [c.kwargs.get("step") for c in mock_log.call_args_list])


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
		self.assertIn(pe_creator.STEP_UNEXPECTED_ERROR, [c.kwargs.get("step") for c in mock_log.call_args_list])


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
