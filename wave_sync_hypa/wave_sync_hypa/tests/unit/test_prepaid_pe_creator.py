"""Unit tests for services.prepaid_pe_creator (issue #193).

Two phases: (A) ensure_draft_pe_for_order builds an UNALLOCATED draft at SO
confirm/verify-success; (B) attach_and_submit_for_si attaches the Sales Invoice
and submits when reconciled. Covers the OR-anchor dedup (reference_no /
wave_order_id / wave_friendly_id), the per-transaction lock, the concurrent
n8n-duplicate + submitted-PE conflict alarms, the amount gate, the button
envelope, and the worker gates. ERPNext get_payment_entry, frappe.get_doc /
db reads, the filelock, and the alarm helpers are patched at the module boundary.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import nowdate
from frappe.utils.file_lock import LockTimeoutError

from wave_sync_hypa.wave_sync_hypa.services import prepaid_pe_creator as pe_creator

SI = "ACC-SINV-2026-00105"
SO = "SAL-ORD-2026-00105"
TXN = "7799531096406646604275"
WOID = "6a17ed37a7685d8ebbf3f9a6"
FID = "10000105"


def _settings(auto=1):
	s = MagicMock(name="WaveSettings")
	s.get.side_effect = lambda key, default=None: {
		"ipay_auto_create_payment_entry": auto,
		"payment_method_mappings": [],
		"wave_payment_review_assignee": "accountant@example.com",
	}.get(key, default)
	return s


def _si_row(docstatus=1, is_return=0, customer="Cust", outstanding=260.0, grand_total=260.0,
	debit_to="Debtors - X", owner="ops@example.com"):
	return frappe._dict(
		docstatus=docstatus, is_return=is_return, customer=customer,
		outstanding_amount=outstanding, grand_total=grand_total, debit_to=debit_to, owner=owner,
	)


def _so_row(docstatus=1, classification="prepaid", txn=TXN, hold=260.0, additional=0.0, txn_amount=260.0):
	return frappe._dict(
		docstatus=docstatus, customer="Cust", company="Co",
		wave_payment_classification=classification, wave_ipay_transaction_code=txn,
		wave_ipay_paid_at="2026-05-28 10:25:10", wave_ipay_transaction_amount=txn_amount,
		wave_payment_type="card", wave_payment_hold=hold, wave_additional_payment_hold=additional,
		wave_order_id=WOID, wave_friendly_id=FID,
	)


def _source(hold=260.0, additional=0.0):
	return [{
		"so": SO, "transaction_code": TXN, "paid_at": "2026-05-28 10:25:10", "payment_type": "card",
		"wave_payment_hold": hold, "wave_additional_payment_hold": additional,
		"wave_order_id": WOID, "wave_friendly_id": FID,
	}]


@contextlib.contextmanager
def _nolock(*a, **k):
	yield


class TestEnsureDraft(FrappeTestCase):
	"""Phase A — unallocated draft at SO confirm; lock patched out."""

	def setUp(self):
		p = patch.object(pe_creator, "filelock", _nolock)
		p.start()
		self.addCleanup(p.stop)

	def test_skips_non_prepaid_or_unsubmitted(self):
		for row in (_so_row(classification="cod"), _so_row(docstatus=0)):
			with (
				patch.object(frappe.db, "get_value", return_value=row),
				patch.object(pe_creator, "_build_unallocated_draft") as mock_build,
			):
				pe_creator.ensure_draft_pe_for_order(SO, "c", settings=_settings())
			mock_build.assert_not_called()

	def test_defers_when_no_transaction_code(self):
		with (
			patch.object(frappe.db, "get_value", side_effect=[_so_row(txn=""), ""]),
			patch.object(pe_creator.ipay_payment_sync, "fetch_and_stamp"),
			patch.object(pe_creator, "_build_unallocated_draft") as mock_build,
			patch.object(pe_creator, "log_step") as mock_log,
		):
			pe_creator.ensure_draft_pe_for_order(SO, "c", settings=_settings())
		mock_build.assert_not_called()
		self.assertIn(pe_creator.STEP_NO_TXN_CODE, [c.kwargs.get("step") for c in mock_log.call_args_list])

	def test_idempotent_when_pe_already_exists(self):
		with (
			patch.object(frappe.db, "get_value", return_value=_so_row()),
			patch.object(pe_creator, "_find_pe_for_order", return_value=("ACC-PAY-0001", 0)),
			patch.object(pe_creator, "_build_unallocated_draft") as mock_build,
			patch.object(pe_creator, "log_step") as mock_log,
		):
			pe_creator.ensure_draft_pe_for_order(SO, "c", settings=_settings())
		mock_build.assert_not_called()
		self.assertIn(pe_creator.STEP_DRAFT_EXISTS, [c.kwargs.get("step") for c in mock_log.call_args_list])

	def test_builds_unallocated_draft_with_payment_details(self):
		fake_pe = MagicMock(name="PE")
		fake_pe.name = "ACC-PAY-DRAFT"
		with (
			patch.object(frappe.db, "get_value", return_value=_so_row()),
			patch.object(pe_creator, "_find_pe_for_order", return_value=None),
			patch.object(frappe, "new_doc", return_value=fake_pe),
			patch.object(pe_creator.payment_mapping, "mode_of_payment_for", return_value="MPESA"),
			patch.object(pe_creator, "_bank_account_for", return_value="Cash - X"),
			patch.object(pe_creator, "log_step") as mock_log,
		):
			pe_creator.ensure_draft_pe_for_order(SO, "c", settings=_settings())
		self.assertEqual(fake_pe.payment_type, "Receive")
		self.assertEqual(fake_pe.party, "Cust")
		self.assertEqual(fake_pe.paid_amount, 260.0)
		self.assertEqual(fake_pe.mode_of_payment, "MPESA")
		self.assertEqual(fake_pe.paid_to, "Cash - X")
		self.assertEqual(fake_pe.reference_no, TXN)
		self.assertEqual(fake_pe.wave_order_id, WOID)
		self.assertEqual(fake_pe.wave_friendly_id, FID)
		self.assertEqual(fake_pe.posting_date, frappe.utils.nowdate())
		# insert() runs the validate chain; we must NOT call set_missing_values() on
		# a brand-new doc (it raises before party_account is set up).
		fake_pe.set_missing_values.assert_not_called()
		fake_pe.insert.assert_called_once()
		self.assertIn(pe_creator.STEP_DRAFT_CREATED, [c.kwargs.get("step") for c in mock_log.call_args_list])

	def test_defers_when_mode_of_payment_has_no_account(self):
		with (
			patch.object(frappe.db, "get_value", return_value=_so_row()),
			patch.object(pe_creator, "_find_pe_for_order", return_value=None),
			patch.object(pe_creator.payment_mapping, "mode_of_payment_for", return_value="MPESA"),
			patch.object(pe_creator, "_bank_account_for", return_value=None),
			patch.object(frappe, "new_doc") as mock_new,
			patch.object(pe_creator, "log_step") as mock_log,
		):
			pe_creator.ensure_draft_pe_for_order(SO, "c", settings=_settings())
		mock_new.assert_not_called()
		self.assertIn(pe_creator.STEP_NO_MOP_ACCOUNT, [c.kwargs.get("step") for c in mock_log.call_args_list])


class TestAttachAndSubmit(FrappeTestCase):
	"""Phase B — find/attach/create/submit; deduped by the OR-anchor; lock patched out."""

	def setUp(self):
		p = patch.object(pe_creator, "filelock", _nolock)
		p.start()
		self.addCleanup(p.stop)

	def test_skips_non_prepaid_invoice(self):
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row()),
			patch.object(pe_creator, "_prepaid_sources", return_value=[]),
			patch.object(pe_creator, "log_step") as mock_log,
		):
			res = pe_creator.attach_and_submit_for_si(SI, "c", settings=_settings())
		self.assertFalse(res["ok"])
		self.assertIn(pe_creator.STEP_SKIPPED_NOT_PREPAID, [c.kwargs.get("step") for c in mock_log.call_args_list])

	def test_multi_source_flags(self):
		two = _source() + [{"so": "SO-2", "transaction_code": "T2", "paid_at": None, "payment_type": "card",
			"wave_payment_hold": 0, "wave_additional_payment_hold": 0, "wave_order_id": "w2", "wave_friendly_id": "f2"}]
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row()),
			patch.object(pe_creator, "_prepaid_sources", return_value=two),
			patch.object(pe_creator.payment_review_flag, "flag") as mock_flag,
			patch.object(pe_creator, "log_step"),
		):
			res = pe_creator.attach_and_submit_for_si(SI, "c", settings=_settings())
		self.assertFalse(res["ok"])
		mock_flag.assert_called_once()

	def test_lock_timeout_flags(self):
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row()),
			patch.object(pe_creator, "_prepaid_sources", return_value=_source()),
			patch.object(pe_creator, "filelock", side_effect=LockTimeoutError("busy")),
			patch.object(pe_creator.payment_review_flag, "flag") as mock_flag,
			patch.object(pe_creator, "log_step") as mock_log,
		):
			res = pe_creator.attach_and_submit_for_si(SI, "c", settings=_settings())
		self.assertFalse(res["ok"])
		mock_flag.assert_called_once()
		self.assertIn(pe_creator.STEP_LOCK_TIMEOUT, [c.kwargs.get("step") for c in mock_log.call_args_list])

	def test_creates_and_submits_when_amount_reconciles(self):
		fake_pe = MagicMock(name="PE")
		fake_pe.name = "ACC-PAY-0001"
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row(grand_total=260.0)),
			patch.object(frappe.db, "set_value") as mock_set,
			patch.object(pe_creator, "_prepaid_sources", return_value=_source(hold=260.0)),
			patch.object(pe_creator, "_find_pe_for_order", return_value=None),
			patch.object(pe_creator, "_other_live_pes_for_order", return_value=[]),
			patch.object(pe_creator, "get_payment_entry", return_value=fake_pe),
			patch.object(pe_creator.payment_review_flag, "clear") as mock_clear,
			patch.object(pe_creator, "log_step") as mock_log,
		):
			res = pe_creator.attach_and_submit_for_si(SI, "c", settings=_settings())
		self.assertTrue(res["ok"])
		self.assertTrue(res["created"])
		self.assertEqual(fake_pe.reference_no, TXN)
		self.assertEqual(fake_pe.wave_order_id, WOID)
		fake_pe.insert.assert_called_once()
		fake_pe.submit.assert_called_once()
		mock_clear.assert_called_once()
		# stamps the SI with the PE for button visibility
		mock_set.assert_any_call("Sales Invoice", SI, "wave_payment_entry", "ACC-PAY-0001", update_modified=False)
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pe_creator.STEP_CREATED, steps)
		self.assertIn(pe_creator.STEP_SUBMITTED, steps)

	def test_amount_mismatch_creates_draft_but_does_not_submit(self):
		fake_pe = MagicMock(name="PE")
		fake_pe.name = "ACC-PAY-0009"
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row(grand_total=250.0)),
			patch.object(pe_creator, "_prepaid_sources", return_value=_source(hold=260.0)),
			patch.object(pe_creator, "_find_pe_for_order", return_value=None),
			patch.object(pe_creator, "_other_live_pes_for_order", return_value=[]),
			patch.object(pe_creator, "get_payment_entry", return_value=fake_pe),
			patch.object(pe_creator.payment_review_flag, "flag") as mock_flag,
			patch.object(pe_creator, "log_step") as mock_log,
		):
			res = pe_creator.attach_and_submit_for_si(SI, "c", settings=_settings())
		self.assertFalse(res["ok"])
		fake_pe.insert.assert_called_once()
		fake_pe.submit.assert_not_called()
		mock_flag.assert_called_once()
		self.assertIn(pe_creator.STEP_AMOUNT_MISMATCH, [c.kwargs.get("step") for c in mock_log.call_args_list])

	def test_missing_hold_creates_draft_but_does_not_submit(self):
		fake_pe = MagicMock(name="PE")
		fake_pe.name = "ACC-PAY-NOHOLD"
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row(grand_total=260.0)),
			patch.object(pe_creator, "_prepaid_sources", return_value=_source(hold=0.0)),
			patch.object(pe_creator, "_find_pe_for_order", return_value=None),
			patch.object(pe_creator, "_other_live_pes_for_order", return_value=[]),
			patch.object(pe_creator, "get_payment_entry", return_value=fake_pe),
			patch.object(pe_creator.payment_review_flag, "flag") as mock_flag,
			patch.object(pe_creator, "log_step") as mock_log,
		):
			res = pe_creator.attach_and_submit_for_si(SI, "c", settings=_settings())
		self.assertFalse(res["ok"])
		fake_pe.submit.assert_not_called()
		mock_flag.assert_called_once()
		self.assertIn(pe_creator.STEP_AMOUNT_MISMATCH, [c.kwargs.get("step") for c in mock_log.call_args_list])

	def test_concurrent_duplicate_detected_alarms(self):
		fake_pe = MagicMock(name="PE")
		fake_pe.name = "ACC-PAY-OURS"
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row()),
			patch.object(pe_creator, "_prepaid_sources", return_value=_source()),
			patch.object(pe_creator, "_find_pe_for_order", return_value=None),
			patch.object(pe_creator, "_other_live_pes_for_order", return_value=["ACC-PAY-N8N"]),
			patch.object(pe_creator, "get_payment_entry", return_value=fake_pe),
			patch.object(pe_creator, "_comment_on_pe") as mock_comment,
			patch.object(pe_creator, "_assign_pe"),
			patch.object(pe_creator, "_notify_si_owner") as mock_notify,
			patch.object(pe_creator.payment_review_flag, "flag") as mock_flag,
			patch.object(pe_creator, "log_step") as mock_log,
		):
			res = pe_creator.attach_and_submit_for_si(SI, "c", settings=_settings())
		self.assertFalse(res["ok"])
		fake_pe.submit.assert_not_called()
		mock_flag.assert_called_once()
		mock_notify.assert_called_once()
		self.assertEqual({c.args[0] for c in mock_comment.call_args_list}, {"ACC-PAY-OURS", "ACC-PAY-N8N"})
		self.assertIn(pe_creator.STEP_DUPLICATE_DETECTED, [c.kwargs.get("step") for c in mock_log.call_args_list])

	def test_attaches_existing_draft_and_submits(self):
		draft = MagicMock(name="DraftPE")
		draft.name = "ACC-PAY-DRAFT"
		draft.references = []
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row(grand_total=260.0)),
			patch.object(frappe.db, "set_value"),
			patch.object(pe_creator, "_prepaid_sources", return_value=_source(hold=260.0)),
			patch.object(pe_creator, "_find_pe_for_order", return_value=("ACC-PAY-DRAFT", 0)),
			patch.object(frappe, "get_doc", return_value=draft),
			patch.object(pe_creator, "_other_live_pes_for_order", return_value=[]),
			patch.object(pe_creator.payment_review_flag, "clear"),
			patch.object(pe_creator, "log_step") as mock_log,
		):
			res = pe_creator.attach_and_submit_for_si(SI, "c", settings=_settings())
		self.assertTrue(res["ok"])
		self.assertFalse(res["created"])
		self.assertEqual(draft.party, "Cust")
		self.assertEqual(draft.paid_from, "Debtors - X")     # aligned to SI.debit_to
		draft.append.assert_called_once()
		self.assertEqual(draft.append.call_args.args[0], "references")
		draft.set_missing_ref_details.assert_called_once_with(force=True)
		draft.save.assert_called_once()
		draft.submit.assert_called_once()
		self.assertIn(pe_creator.STEP_UPDATED_DRAFT, [c.kwargs.get("step") for c in mock_log.call_args_list])

	def test_attach_draft_detects_concurrent_duplicate(self):
		"""A second live PE for the order during draft-attach -> alarm, never submit."""
		draft = MagicMock(name="DraftPE")
		draft.name = "ACC-PAY-DRAFT"
		draft.references = []
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row(grand_total=260.0)),
			patch.object(pe_creator, "_prepaid_sources", return_value=_source(hold=260.0)),
			patch.object(pe_creator, "_find_pe_for_order", return_value=("ACC-PAY-DRAFT", 0)),
			patch.object(frappe, "get_doc", return_value=draft),
			patch.object(pe_creator, "_other_live_pes_for_order", return_value=["ACC-PAY-N8N"]),
			patch.object(pe_creator, "_raise_duplicate_alarm") as mock_alarm,
			patch.object(pe_creator, "log_step"),
		):
			res = pe_creator.attach_and_submit_for_si(SI, "c", settings=_settings())
		self.assertFalse(res["ok"])
		draft.submit.assert_not_called()
		mock_alarm.assert_called_once()

	def test_submitted_pe_already_referencing_si_is_idempotent(self):
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row()),
			patch.object(frappe.db, "set_value") as mock_set,
			patch.object(pe_creator, "_prepaid_sources", return_value=_source()),
			patch.object(pe_creator, "_find_pe_for_order", return_value=("ACC-PAY-SUB", 1)),
			patch.object(pe_creator, "_pe_references_si", return_value=True),
			patch.object(pe_creator.payment_review_flag, "flag") as mock_flag,
			patch.object(pe_creator, "log_step") as mock_log,
		):
			res = pe_creator.attach_and_submit_for_si(SI, "c", settings=_settings())
		self.assertTrue(res["ok"])
		mock_flag.assert_not_called()
		# Stamps the already-settling PE onto the SI so the button hides.
		mock_set.assert_any_call("Sales Invoice", SI, "wave_payment_entry", "ACC-PAY-SUB", update_modified=False)
		self.assertIn(pe_creator.STEP_ALREADY_SETTLED, [c.kwargs.get("step") for c in mock_log.call_args_list])

	def test_submitted_pe_conflict_raises_full_alarm(self):
		with (
			patch.object(frappe.db, "get_value", return_value=_si_row()),
			patch.object(pe_creator, "_prepaid_sources", return_value=_source()),
			patch.object(pe_creator, "_find_pe_for_order", return_value=("ACC-PAY-SUB", 1)),
			patch.object(pe_creator, "_pe_references_si", return_value=False),
			patch.object(pe_creator, "_comment_on_pe") as mock_comment,
			patch.object(pe_creator, "_assign_pe") as mock_assign,
			patch.object(pe_creator, "_notify_si_owner") as mock_notify,
			patch.object(pe_creator.payment_review_flag, "flag") as mock_flag,
			patch.object(pe_creator, "log_step") as mock_log,
		):
			res = pe_creator.attach_and_submit_for_si(SI, "c", settings=_settings())
		self.assertFalse(res["ok"])
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
			patch.object(pe_creator, "_prepaid_sources", return_value=_source(hold=260.0)),
			patch.object(pe_creator, "_find_pe_for_order", return_value=None),
			patch.object(pe_creator, "_other_live_pes_for_order", return_value=[]),
			patch.object(pe_creator, "get_payment_entry", return_value=fake_pe),
			patch.object(pe_creator.payment_review_flag, "flag") as mock_flag,
			patch.object(pe_creator, "log_step") as mock_log,
		):
			res = pe_creator.attach_and_submit_for_si(SI, "c", settings=_settings())
		self.assertFalse(res["ok"])
		fake_pe.insert.assert_called_once()
		mock_flag.assert_called_once()
		self.assertIn(pe_creator.STEP_SUBMIT_BLOCKED, [c.kwargs.get("step") for c in mock_log.call_args_list])


class TestDedupAnchors(FrappeTestCase):
	"""The OR-anchor identifies a PE by reference_no OR wave_order_id OR wave_friendly_id."""

	def test_find_pe_for_order_passes_all_three_anchors(self):
		captured = {}

		def _capture(doctype, **kwargs):
			captured.update(kwargs)
			return []

		with patch.object(frappe, "get_all", side_effect=_capture):
			pe_creator._find_pe_for_order(TXN, WOID, FID)
		anchors = captured["or_filters"]
		self.assertIn(["reference_no", "=", TXN], anchors)
		self.assertIn(["wave_order_id", "=", WOID], anchors)
		self.assertIn(["wave_friendly_id", "=", FID], anchors)

	def test_anchors_omit_missing_wave_ids(self):
		self.assertEqual(pe_creator._order_anchors(TXN, None, None), [["reference_no", "=", TXN]])


class TestButtonEntry(FrappeTestCase):
	"""find_or_create_for_si: gated, returns the uniform envelope, delegates to the engine."""

	def test_disabled_when_flag_off(self):
		with (
			patch.object(pe_creator, "is_wave_integration_enabled", return_value=True),
			patch.object(frappe, "get_cached_doc", return_value=_settings(auto=0)),
			patch.object(pe_creator, "attach_and_submit_for_si") as mock_core,
		):
			res = pe_creator.find_or_create_for_si(SI, "c")
		self.assertFalse(res["ok"])
		mock_core.assert_not_called()

	def test_delegates_when_enabled(self):
		envelope = pe_creator._result(True, created=True, payment_entry="ACC-PAY-X", docstatus=1)
		with (
			patch.object(pe_creator, "is_wave_integration_enabled", return_value=True),
			patch.object(frappe, "get_cached_doc", return_value=_settings(auto=1)),
			patch.object(pe_creator, "attach_and_submit_for_si", return_value=envelope) as mock_core,
		):
			res = pe_creator.find_or_create_for_si(SI, "c")
		self.assertTrue(res["ok"])
		self.assertEqual(res["payment_entry"], "ACC-PAY-X")
		mock_core.assert_called_once()


class TestEnqueueGuard(FrappeTestCase):
	"""A queue-backend failure must never propagate out of the submit hot path."""

	def test_enqueue_failure_is_swallowed_and_logged(self):
		with (
			patch.object(frappe, "enqueue", side_effect=RuntimeError("redis down")),
			patch.object(pe_creator, "log_step") as mock_log,
		):
			pe_creator.enqueue_draft_for_order(SO, "c")  # must not raise
			pe_creator.enqueue_attach_for_si(SI, "c")    # must not raise
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertEqual(steps.count(pe_creator.STEP_ENQUEUE_FAILED), 2)
		self.assertNotIn(pe_creator.STEP_DRAFT_ENQUEUED, steps)


class TestWorkerGates(FrappeTestCase):
	"""Both workers backstop the master switch + the auto-create flag, and swallow exceptions."""

	def test_draft_worker_skips_when_disabled(self):
		with (
			patch.object(pe_creator, "_enabled_settings", return_value=None),
			patch.object(pe_creator, "ensure_draft_pe_for_order") as mock_core,
		):
			pe_creator.ensure_draft_pe_worker(sales_order=SO, correlation_id="c")
		mock_core.assert_not_called()

	def test_attach_worker_skips_when_disabled(self):
		with (
			patch.object(pe_creator, "_enabled_settings", return_value=None),
			patch.object(pe_creator, "attach_and_submit_for_si") as mock_core,
		):
			pe_creator.attach_and_submit_worker(sales_invoice=SI, correlation_id="c")
		mock_core.assert_not_called()

	def test_attach_worker_calls_core_when_enabled(self):
		with (
			patch.object(pe_creator, "_enabled_settings", return_value=_settings(auto=1)),
			patch.object(pe_creator, "attach_and_submit_for_si") as mock_core,
		):
			pe_creator.attach_and_submit_worker(sales_invoice=SI, correlation_id="c")
		mock_core.assert_called_once()

	def test_worker_swallows_unexpected_error(self):
		with (
			patch.object(pe_creator, "_enabled_settings", side_effect=RuntimeError("boom")),
			patch.object(pe_creator, "log_step") as mock_log,
		):
			pe_creator.attach_and_submit_worker(sales_invoice=SI, correlation_id="c")  # no raise
		self.assertIn(pe_creator.STEP_UNEXPECTED_ERROR, [c.kwargs.get("step") for c in mock_log.call_args_list])
