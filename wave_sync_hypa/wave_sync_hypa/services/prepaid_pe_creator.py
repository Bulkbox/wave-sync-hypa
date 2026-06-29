"""Create a prepaid order's Payment Entry in-app, replacing the external n8n flow.

Two phases, both gated by Wave Settings.ipay_auto_create_payment_entry (off by
default, so n8n stays authoritative until an admin opts in):

  * SO confirm / a successful "Verify iPay Payment": once iPay has confirmed the
    payment for a submitted prepaid Sales Order, create an UNALLOCATED draft
    Payment Entry carrying the payment details (party, mode_of_payment,
    reference_no = the iPay transaction code, reference_date = paid-at, amount =
    the Wave-authorised hold) with `references = []`. We stamp wave_order_id /
    wave_friendly_id on it so it is findable.

  * SI submit / the "Wave Payment Entry" button: find that draft, attach the
    Sales Invoice, and submit it when the invoice total reconciles with the hold
    (within FULL_PAYMENT_TOLERANCE); otherwise leave it a draft and flag the
    invoice for manual review.

One idempotent engine serves the SO worker, the SI worker, and the button. A PE
"belongs to" an order when ANY of these match (the dedup OR-anchor): its
references reach the SI/SO, it carries the order's wave_order_id/wave_friendly_id,
or its reference_no equals the iPay transaction code — n8n's allocated PEs are
caught by the stamped wave ids (our validate hook stamps them), its unallocated
drafts by reference_no. Create / attach / submit run under a per-transaction file
lock; before submitting we re-check for a second live PE (a concurrent n8n
create the lock can't see) and alarm instead of double-settling. The worker and
button paths never raise.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt, getdate, nowdate
from frappe.utils.file_lock import LockTimeoutError
from frappe.utils.synchronization import filelock

from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
from erpnext.accounts.doctype.sales_invoice.sales_invoice import get_bank_cash_account

from wave_sync_hypa.wave_sync_hypa.services import ipay_payment_sync, payment_mapping, payment_review_flag
from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step
from wave_sync_hypa.wave_sync_hypa.services.master_switch import is_wave_integration_enabled
from wave_sync_hypa.wave_sync_hypa.services.payment_status_resolver import FULL_PAYMENT_TOLERANCE

DRAFT_WORKER_DOTTED_PATH = (
	"wave_sync_hypa.wave_sync_hypa.services.prepaid_pe_creator.ensure_draft_pe_worker"
)
ATTACH_WORKER_DOTTED_PATH = (
	"wave_sync_hypa.wave_sync_hypa.services.prepaid_pe_creator.attach_and_submit_worker"
)

# Per-transaction lock so two of our own paths can't both create a PE for one
# iPay transaction. Held only for the brief find-or-create window.
LOCK_TIMEOUT_SECONDS = 30

# Marker on PE timeline Comments we add, so the conflict note is idempotent
# (a retry never spams the same PE with duplicate alarms).
_PE_CONFLICT_MARKER = "Wave Sync — iPay transaction conflict"

STEP_DRAFT_ENQUEUED = "prepaid_pe_draft_enqueued"
STEP_ATTACH_ENQUEUED = "prepaid_pe_attach_enqueued"
STEP_SKIPPED_NOT_PREPAID = "prepaid_pe_skipped_not_prepaid"
STEP_MULTI_SOURCE = "prepaid_pe_multi_source_flagged"
STEP_NO_TXN_CODE = "prepaid_pe_no_transaction_code"
STEP_NO_AMOUNT = "prepaid_pe_no_authorised_amount"
STEP_NO_MOP_ACCOUNT = "prepaid_pe_no_mode_of_payment_account"
STEP_DRAFT_CREATED = "prepaid_pe_draft_created"
STEP_DRAFT_EXISTS = "prepaid_pe_draft_already_exists"
STEP_DRAFT_BUILD_FAILED = "prepaid_pe_draft_build_failed"
STEP_UPDATED_DRAFT = "prepaid_pe_updated_existing_draft"
STEP_ALREADY_SETTLED = "prepaid_pe_already_settled"
STEP_BLOCKED_SUBMITTED_PE = "prepaid_pe_blocked_submitted_pe"
STEP_DUPLICATE_DETECTED = "prepaid_pe_duplicate_detected"
STEP_AMOUNT_MISMATCH = "prepaid_pe_amount_mismatch_draft_left"
STEP_LOCK_TIMEOUT = "prepaid_pe_lock_timeout"
STEP_CREATED = "prepaid_pe_created"
STEP_SUBMIT_BLOCKED = "prepaid_pe_submit_blocked"
STEP_SUBMITTED = "prepaid_pe_submitted"
STEP_UNEXPECTED_ERROR = "prepaid_pe_unexpected_error"


def _result(ok, *, created=False, payment_entry=None, docstatus=None, reason=None) -> dict:
	"""Uniform envelope returned to the button (and ignored by the fire-and-forget workers)."""
	return {"ok": ok, "created": created, "payment_entry": payment_entry, "docstatus": docstatus, "reason": reason}


# --------------------------------------------------------------------------- #
# Phase A — unallocated draft at SO confirm / verify-success
# --------------------------------------------------------------------------- #
def enqueue_draft_for_order(sales_order: str, correlation_id: str) -> None:
	"""Queue the unallocated-draft creation for a confirmed prepaid SO (after_commit)."""
	frappe.enqueue(
		DRAFT_WORKER_DOTTED_PATH,
		queue="default",
		enqueue_after_commit=True,
		job_name=f"prepaid_pe_draft:{sales_order}",
		sales_order=sales_order,
		correlation_id=correlation_id,
	)
	log_step(
		correlation_id=correlation_id, step=STEP_DRAFT_ENQUEUED, level="Info",
		doc_type="Sales Order", linked_doctype="Sales Order", linked_docname=sales_order,
	)


def ensure_draft_pe_worker(*, sales_order: str, correlation_id: str) -> None:
	"""Async entry: master-switch + feature-flag backstop, then ensure the draft. Never raises."""
	try:
		settings = _enabled_settings()
		if settings:
			ensure_draft_pe_for_order(sales_order, correlation_id, settings=settings)
	except Exception as exc:
		_log_unexpected("Sales Order", sales_order, correlation_id, exc, "ensure_draft_pe_worker")


def ensure_draft_pe_for_order(so_name: str, correlation_id: str, *, settings=None) -> None:
	"""Create the unallocated draft PE for a submitted, iPay-verified prepaid SO. Idempotent."""
	settings = settings or frappe.get_cached_doc("Wave Settings")
	so = frappe.db.get_value(
		"Sales Order", so_name,
		[
			"docstatus", "customer", "company", "wave_payment_classification",
			"wave_ipay_transaction_code", "wave_ipay_paid_at", "wave_ipay_transaction_amount",
			"wave_payment_type", "wave_payment_hold", "wave_additional_payment_hold",
			"wave_order_id", "wave_friendly_id",
		],
		as_dict=True,
	)
	if not so or so.docstatus != 1 or (so.wave_payment_classification or "") != "prepaid":
		return

	txn = (so.wave_ipay_transaction_code or "").strip()
	if not txn:
		# iPay may have been slow at intake; try once more, then re-read.
		ipay_payment_sync.fetch_and_stamp(so_name, correlation_id, settings=settings)
		txn = (frappe.db.get_value("Sales Order", so_name, "wave_ipay_transaction_code") or "").strip()
	if not txn:
		log_step(
			correlation_id=correlation_id, step=STEP_NO_TXN_CODE, level="Info",
			doc_type="Sales Order", linked_doctype="Sales Order", linked_docname=so_name,
			error_message="No verified iPay payment yet; draft Payment Entry deferred.",
		)
		return

	try:
		with filelock(_lock_name(txn), timeout=LOCK_TIMEOUT_SECONDS):
			if _find_pe_for_order(txn, so.get("wave_order_id"), so.get("wave_friendly_id")):
				log_step(
					correlation_id=correlation_id, step=STEP_DRAFT_EXISTS, level="Info",
					doc_type="Sales Order", linked_doctype="Sales Order", linked_docname=so_name,
					request_body={"transaction_code": txn},
				)
				return
			_build_unallocated_draft(so_name, so, txn, settings, correlation_id)
	except LockTimeoutError:
		log_step(
			correlation_id=correlation_id, step=STEP_LOCK_TIMEOUT, level="Info",
			doc_type="Sales Order", linked_doctype="Sales Order", linked_docname=so_name,
			error_message=f"Another process is settling iPay transaction {txn}; draft skipped, re-check later.",
		)


def _build_unallocated_draft(so_name, so, txn, settings, correlation_id) -> None:
	"""Insert an unallocated Receive PE carrying the iPay details; references=[]."""
	amount = flt(so.get("wave_payment_hold")) + flt(so.get("wave_additional_payment_hold"))
	if amount <= 0:
		amount = flt(so.get("wave_ipay_transaction_amount"))
	if amount <= 0:
		log_step(
			correlation_id=correlation_id, step=STEP_NO_AMOUNT, level="Info",
			doc_type="Sales Order", linked_doctype="Sales Order", linked_docname=so_name,
			error_message="No Wave-authorised amount; draft Payment Entry deferred.",
		)
		return

	mop = payment_mapping.mode_of_payment_for(settings, so.get("wave_payment_type"))
	paid_to = _bank_account_for(mop, so.get("company")) if mop else None
	if not mop or not paid_to:
		log_step(
			correlation_id=correlation_id, step=STEP_NO_MOP_ACCOUNT, level="Info",
			doc_type="Sales Order", linked_doctype="Sales Order", linked_docname=so_name,
			error_message=f"Mode of Payment for {so.get('wave_payment_type')!r} unmapped or has no company account; "
			"draft Payment Entry deferred (it will be built at Sales Invoice time).",
		)
		return

	try:
		pe = frappe.new_doc("Payment Entry")
		pe.payment_type = "Receive"
		pe.company = so.get("company")
		pe.posting_date = nowdate()
		pe.party_type = "Customer"
		pe.party = so.get("customer")
		pe.paid_amount = amount
		pe.received_amount = amount
		pe.mode_of_payment = mop
		pe.paid_to = paid_to
		pe.reference_no = txn
		pe.reference_date = _reference_date(so.get("wave_ipay_paid_at"))
		pe.wave_order_id = so.get("wave_order_id")
		pe.wave_friendly_id = so.get("wave_friendly_id")
		# insert() runs the validate chain (setup_party_account_field ->
		# set_missing_values), deriving paid_from (the receivable) while preserving
		# references=[] and the stamped wave ids. Calling set_missing_values()
		# here would raise on a brand-new doc (party_account not yet set up).
		pe.insert(ignore_permissions=True)
	except Exception as exc:
		log_step(
			correlation_id=correlation_id, step=STEP_DRAFT_BUILD_FAILED, level="Warning",
			doc_type="Sales Order", linked_doctype="Sales Order", linked_docname=so_name,
			error_message=f"Could not build the unallocated draft Payment Entry for iPay transaction {txn}: {exc}",
		)
		return

	log_step(
		correlation_id=correlation_id, step=STEP_DRAFT_CREATED, level="Success",
		doc_type="Sales Order", linked_doctype="Payment Entry", linked_docname=pe.name,
		request_body={"sales_order": so_name, "transaction_code": txn, "amount": amount},
	)


# --------------------------------------------------------------------------- #
# Phase B — attach the Sales Invoice and submit (SI submit / button)
# --------------------------------------------------------------------------- #
def enqueue_attach_for_si(sales_invoice: str, correlation_id: str) -> None:
	"""Queue the attach-and-submit for a submitted prepaid SI (after_commit)."""
	frappe.enqueue(
		ATTACH_WORKER_DOTTED_PATH,
		queue="default",
		enqueue_after_commit=True,
		job_name=f"prepaid_pe_attach:{sales_invoice}",
		sales_invoice=sales_invoice,
		correlation_id=correlation_id,
	)
	log_step(
		correlation_id=correlation_id, step=STEP_ATTACH_ENQUEUED, level="Info",
		doc_type="Sales Invoice", linked_doctype="Sales Invoice", linked_docname=sales_invoice,
	)


def attach_and_submit_worker(*, sales_invoice: str, correlation_id: str) -> None:
	"""Async entry: master-switch + feature-flag backstop, then settle the PE. Never raises."""
	try:
		settings = _enabled_settings()
		if settings:
			attach_and_submit_for_si(sales_invoice, correlation_id, settings=settings)
	except Exception as exc:
		_log_unexpected("Sales Invoice", sales_invoice, correlation_id, exc, "attach_and_submit_worker")


def find_or_create_for_si(si_name: str, correlation_id: str | None = None) -> dict:
	"""Button / sync path: ensure the prepaid PE for this SI exists and is submitted. Returns an envelope."""
	correlation_id = correlation_id or new_correlation_id()
	if not is_wave_integration_enabled():
		return _result(False, reason="Wave integration is disabled.")
	settings = frappe.get_cached_doc("Wave Settings")
	if not settings.get("ipay_auto_create_payment_entry"):
		return _result(False, reason="App-side prepaid Payment Entry creation is disabled in Wave Settings.")
	return attach_and_submit_for_si(si_name, correlation_id, settings=settings)


def attach_and_submit_for_si(si_name: str, correlation_id: str, *, settings=None) -> dict:
	"""Find-attach-submit / create the Payment Entry for a prepaid SI. Our data wins."""
	settings = settings or frappe.get_cached_doc("Wave Settings")
	si = frappe.db.get_value(
		"Sales Invoice", si_name,
		["docstatus", "is_return", "customer", "outstanding_amount", "grand_total", "debit_to", "owner"],
		as_dict=True,
	)
	if not si or si.docstatus != 1 or si.is_return:
		return _result(False, reason="Sales Invoice is not a submitted, non-return invoice.")

	sources = _prepaid_sources(si_name)
	if not sources:
		log_step(
			correlation_id=correlation_id, step=STEP_SKIPPED_NOT_PREPAID, level="Info",
			doc_type="Sales Invoice", linked_doctype="Sales Invoice", linked_docname=si_name,
		)
		return _result(False, reason="Not a prepaid Wave order.")
	if len(sources) > 1:
		_flag(
			si_name, settings, correlation_id, STEP_MULTI_SOURCE,
			"Sales Invoice draws from multiple prepaid Wave orders; create the Payment Entry(s) manually.",
			request_body={"sales_orders": [s["so"] for s in sources]},
		)
		return _result(False, reason="Sales Invoice draws from multiple prepaid Wave orders.")

	src = sources[0]
	txn = (src.get("transaction_code") or "").strip()
	if not txn:
		ipay_payment_sync.fetch_and_stamp(src["so"], correlation_id, settings=settings)
		txn = (frappe.db.get_value("Sales Order", src["so"], "wave_ipay_transaction_code") or "").strip()
	if not txn:
		_flag(
			si_name, settings, correlation_id, STEP_NO_TXN_CODE,
			"No verified iPay payment (no transaction code) for this prepaid order; cannot create a Payment Entry.",
		)
		return _result(False, reason="No verified iPay payment for this prepaid order.")

	try:
		with filelock(_lock_name(txn), timeout=LOCK_TIMEOUT_SECONDS):
			return _settle_under_lock(si_name, si, src, settings, correlation_id, txn)
	except LockTimeoutError:
		_flag(
			si_name, settings, correlation_id, STEP_LOCK_TIMEOUT,
			f"Another process is already settling iPay transaction {txn}; Payment Entry creation was "
			"skipped to avoid a duplicate. Re-check shortly.",
		)
		return _result(False, reason="Busy settling this transaction; try again shortly.")


def _settle_under_lock(si_name, si, src, settings, correlation_id, txn) -> dict:
	"""Find-or-create branch, run while holding the per-transaction lock."""
	existing = _find_pe_for_order(txn, src.get("wave_order_id"), src.get("wave_friendly_id"))
	if existing:
		name, docstatus = existing
		if docstatus == 0:
			return _update_and_submit_draft(name, si_name, si, src, settings, correlation_id, txn)
		if _pe_references_si(name, si_name):
			log_step(
				correlation_id=correlation_id, step=STEP_ALREADY_SETTLED, level="Info",
				doc_type="Sales Invoice", linked_doctype="Sales Invoice", linked_docname=si_name,
				request_body={"payment_entry": name, "transaction_code": txn},
			)
			return _result(True, created=False, payment_entry=name, docstatus=1, reason="Payment Entry already settles this invoice.")
		_raise_submitted_conflict_alarm(si_name, si, name, txn, settings, correlation_id)
		return _result(False, payment_entry=name, docstatus=1, reason="A submitted Payment Entry already owns this transaction; manual reconciliation required.")

	return _create_and_submit(si_name, si, src, settings, correlation_id, txn)


def _create_and_submit(si_name, si, src, settings, correlation_id, txn) -> dict:
	"""Build a fresh PE from the SI via ERPNext's helper, stamp our fields, submit if reconciled."""
	try:
		pe = get_payment_entry("Sales Invoice", si_name, reference_date=_reference_date(src.get("paid_at")))
		pe.reference_no = txn
		pe.wave_order_id = src.get("wave_order_id")
		pe.wave_friendly_id = src.get("wave_friendly_id")
		mop = payment_mapping.mode_of_payment_for(settings, src.get("payment_type"))
		if mop:
			pe.mode_of_payment = mop
			# Keep the deposit account consistent with the chosen MOP.
			bank = _bank_account_for(mop, pe.company)
			if bank:
				pe.paid_to = bank
		pe.insert(ignore_permissions=True)
		log_step(
			correlation_id=correlation_id, step=STEP_CREATED, level="Info",
			doc_type="Sales Invoice", linked_doctype="Payment Entry", linked_docname=pe.name,
			request_body={"sales_invoice": si_name, "transaction_code": txn},
		)
	except Exception as exc:
		_flag(si_name, settings, correlation_id, STEP_SUBMIT_BLOCKED,
			f"Could not build the Payment Entry for iPay transaction {txn}: {exc}")
		return _result(False, reason=f"Could not build the Payment Entry: {exc}")

	# A second live PE for this order means an external creator (n8n) raced us
	# inside our lock window. Never submit a duplicate — alarm, leave both drafts.
	others = _other_live_pes_for_order(txn, src.get("wave_order_id"), src.get("wave_friendly_id"), exclude=pe.name)
	if others:
		_raise_duplicate_alarm(si_name, si, pe.name, others, txn, settings, correlation_id)
		return _result(False, created=True, payment_entry=pe.name, docstatus=0, reason="A concurrent Payment Entry was detected; reconcile to one.")

	return _submit_if_reconciled(pe, si_name, si, src, settings, correlation_id, txn, created=True)


def _update_and_submit_draft(pe_name, si_name, si, src, settings, correlation_id, txn) -> dict:
	"""Attach this SI to an existing unallocated draft (ours from SO-confirm, or n8n's) and submit if reconciled."""
	try:
		pe = frappe.get_doc("Payment Entry", pe_name)
		pe.party_type = "Customer"
		pe.party = si.customer
		# Align the party (receivable) account to the invoice's debit_to so the
		# appended reference doesn't trip ERPNext's party-account check on submit.
		if si.get("debit_to"):
			pe.paid_from = si.debit_to
			pe.party_account = si.debit_to
		mop = payment_mapping.mode_of_payment_for(settings, src.get("payment_type"))
		if mop:
			pe.mode_of_payment = mop
		pe.reference_no = txn
		pe.reference_date = _reference_date(src.get("paid_at"))
		pe.wave_order_id = src.get("wave_order_id")
		pe.wave_friendly_id = src.get("wave_friendly_id")
		if not _doc_references_si(pe, si_name):
			pe.append("references", {
				"reference_doctype": "Sales Invoice",
				"reference_name": si_name,
				"allocated_amount": flt(si.outstanding_amount),
			})
		pe.set_missing_values()
		# force=True refreshes the reference row's live outstanding/total/exchange,
		# avoiding the "already been partly paid" submit error on stale figures.
		pe.set_missing_ref_details(force=True)
		pe.save(ignore_permissions=True)
		log_step(
			correlation_id=correlation_id, step=STEP_UPDATED_DRAFT, level="Info",
			doc_type="Sales Invoice", linked_doctype="Payment Entry", linked_docname=pe.name,
			request_body={"sales_invoice": si_name, "transaction_code": txn},
		)
	except Exception as exc:
		_flag(si_name, settings, correlation_id, STEP_SUBMIT_BLOCKED,
			f"Could not attach this invoice to draft Payment Entry {pe_name} for iPay transaction {txn}: {exc}")
		return _result(False, payment_entry=pe_name, docstatus=0, reason=f"Could not attach the invoice: {exc}")

	# Same concurrent-creator guard as the create path: never submit when a second
	# live PE (e.g. an n8n draft) shares this order.
	others = _other_live_pes_for_order(txn, src.get("wave_order_id"), src.get("wave_friendly_id"), exclude=pe.name)
	if others:
		_raise_duplicate_alarm(si_name, si, pe.name, others, txn, settings, correlation_id)
		return _result(False, payment_entry=pe.name, docstatus=0, reason="A concurrent Payment Entry was detected; reconcile to one.")
	return _submit_if_reconciled(pe, si_name, si, src, settings, correlation_id, txn, created=False)


def _submit_if_reconciled(pe, si_name, si, src, settings, correlation_id, txn, *, created) -> dict:
	"""Submit only when the invoice total matches the Wave-authorised hold; else leave a draft + flag.

	A missing/zero hold is treated as "cannot verify" (not "no check needed"): a
	prepaid order should carry an authorised amount, so its absence is itself a
	reason to hold the Payment Entry as a draft for manual review.
	"""
	expected = flt(src.get("wave_payment_hold")) + flt(src.get("wave_additional_payment_hold"))
	grand_total = flt(si.grand_total)
	if not expected:
		_flag(
			si_name, settings, correlation_id, STEP_AMOUNT_MISMATCH,
			f"Payment Entry {pe.name} was created as a DRAFT but NOT submitted: no Wave-authorised amount "
			f"(payment hold) is recorded for this prepaid order, so the invoice total {grand_total:.2f} could "
			"not be verified. Verify the iPay payment and submit manually.",
			request_body={"payment_entry": pe.name, "grand_total": grand_total, "expected": expected},
		)
		return _result(False, created=created, payment_entry=pe.name, docstatus=0, reason="No Wave-authorised amount to verify against; left as a draft.")
	if abs(grand_total - expected) >= FULL_PAYMENT_TOLERANCE:
		_flag(
			si_name, settings, correlation_id, STEP_AMOUNT_MISMATCH,
			f"Payment Entry {pe.name} was created as a DRAFT but NOT submitted: invoice total {grand_total:.2f} "
			f"does not match the iPay/Wave authorised amount {expected:.2f} (difference {grand_total - expected:+.2f}). "
			"Review the amounts and submit the Payment Entry manually.",
			request_body={"payment_entry": pe.name, "grand_total": grand_total, "expected": expected},
		)
		return _result(False, created=created, payment_entry=pe.name, docstatus=0, reason="Invoice total does not match the authorised amount; left as a draft.")
	return _submit(pe, si_name, settings, correlation_id, txn, created=created)


def _submit(pe, si_name, settings, correlation_id, txn, *, created) -> dict:
	"""Submit the PE; stamp the SI, clear its review flag on success, flag it on a validator block."""
	try:
		pe.submit()
	except Exception as exc:
		_flag(si_name, settings, correlation_id, STEP_SUBMIT_BLOCKED,
			f"Payment Entry {pe.name} for iPay transaction {txn} could not be submitted: {exc}")
		return _result(False, created=created, payment_entry=pe.name, docstatus=0, reason=f"Payment Entry could not be submitted: {exc}")
	frappe.db.set_value("Sales Invoice", si_name, "wave_payment_entry", pe.name, update_modified=False)
	payment_review_flag.clear("Sales Invoice", si_name, settings=settings, correlation_id=correlation_id)
	log_step(
		correlation_id=correlation_id, step=STEP_SUBMITTED, level="Success",
		doc_type="Sales Invoice", linked_doctype="Payment Entry", linked_docname=pe.name,
		request_body={"sales_invoice": si_name, "transaction_code": txn},
	)
	return _result(True, created=created, payment_entry=pe.name, docstatus=1, reason="Payment Entry submitted.")


# --------------------------------------------------------------------------- #
# Alarms (verbatim from the prepaid-PE design; idempotent, best-effort)
# --------------------------------------------------------------------------- #
def _raise_submitted_conflict_alarm(si_name, si, pe_name, txn, settings, correlation_id) -> None:
	"""A submitted PE owns this txn but not this SI: never touch it; alarm loudly for manual reconciliation."""
	reason = (
		f"iPay transaction {txn} is already settled by submitted Payment Entry {pe_name}, which does NOT "
		f"reference this invoice. A submitted Payment Entry cannot be modified and a second one would "
		"double-settle the payment. Verify the customer was not double-charged and reconcile manually."
	)
	_flag(si_name, settings, correlation_id, STEP_BLOCKED_SUBMITTED_PE, reason,
		request_body={"conflicting_payment_entry": pe_name, "transaction_code": txn})
	_comment_on_pe(
		pe_name,
		f"{_PE_CONFLICT_MARKER}: Sales Invoice {si_name} also maps to iPay transaction {txn}, but this "
		"submitted Payment Entry does not reference it. Expected: one Payment Entry per iPay transaction. "
		"Action: confirm which invoice this payment settles and that the customer was not double-charged; "
		"do NOT create a second Payment Entry. wave_sync_hypa did not modify this entry.",
	)
	_assign_pe(pe_name, settings, f"iPay transaction conflict with {si_name} — verify possible double-charge")
	_notify_si_owner(si_name, si.get("owner"), reason)


def _raise_duplicate_alarm(si_name, si, our_pe, other_pes, txn, settings, correlation_id) -> None:
	"""We created a PE but a concurrent process already had one for this txn: leave drafts, alarm."""
	others = ", ".join(other_pes)
	reason = (
		f"Duplicate Payment Entries detected for iPay transaction {txn}: {our_pe} (created by Wave Sync) "
		f"and {others} (created concurrently, e.g. by n8n). Neither was submitted. Keep ONE, cancel/delete "
		"the rest, and submit the correct one against this invoice."
	)
	_flag(si_name, settings, correlation_id, STEP_DUPLICATE_DETECTED, reason,
		request_body={"our_payment_entry": our_pe, "other_payment_entries": other_pes, "transaction_code": txn})
	for pe_name in [our_pe, *other_pes]:
		_comment_on_pe(
			pe_name,
			f"{_PE_CONFLICT_MARKER}: more than one Payment Entry exists for iPay transaction {txn} "
			f"(this one plus {others or our_pe}). Reconcile to a single Payment Entry for Sales Invoice "
			f"{si_name} before submitting.",
		)
	_assign_pe(our_pe, settings, f"Duplicate iPay Payment Entries for {txn} — reconcile to one")
	_notify_si_owner(si_name, si.get("owner"), reason)


def _comment_on_pe(pe_name: str, body: str) -> None:
	"""Add an idempotent timeline Comment to a Payment Entry; best-effort, never raises."""
	try:
		if frappe.get_all(
			"Comment",
			filters={
				"reference_doctype": "Payment Entry",
				"reference_name": pe_name,
				"comment_type": "Comment",
				"content": ("like", f"%{_PE_CONFLICT_MARKER}%"),
			},
			limit=1,
		):
			return
		frappe.get_doc("Payment Entry", pe_name).add_comment("Comment", body)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "wave_sync_hypa: failed to comment on conflicting PE")


def _assign_pe(pe_name: str, settings, description: str) -> None:
	"""Formally assign the Payment Entry to the payment-review assignee; best-effort, never raises."""
	assignee = (settings.get("wave_payment_review_assignee") or "").strip()
	if not assignee:
		return
	try:
		from frappe.desk.form.assign_to import add as assign_to_add

		assign_to_add(
			{
				"assign_to": [assignee],
				"doctype": "Payment Entry",
				"name": pe_name,
				"description": description,
				"priority": "High",
			},
			ignore_permissions=True,
		)
	except Exception:
		# assign_to raises if already assigned to that user — benign.
		frappe.log_error(frappe.get_traceback(), "wave_sync_hypa: failed to assign conflicting PE")


def _notify_si_owner(si_name: str, owner: str | None, message: str) -> None:
	"""Notify the invoice's creator: a durable Notification Log + a realtime popup. Best-effort."""
	owner = (owner or "").strip()
	if not owner or owner in ("Administrator", "Guest"):
		return
	subject = f"Payment conflict on Sales Invoice {si_name}"
	try:
		frappe.get_doc({
			"doctype": "Notification Log",
			"for_user": owner,
			"type": "Alert",
			"subject": subject,
			"email_content": message,
			"document_type": "Sales Invoice",
			"document_name": si_name,
		}).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "wave_sync_hypa: failed to create Notification Log")
	try:
		# The desk globally listens for the "msgprint" realtime event, so this
		# pops a dialog wherever the user is, if they're online.
		frappe.publish_realtime(
			"msgprint",
			{"title": subject, "message": message, "indicator": "red"},
			user=owner,
			after_commit=True,
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "wave_sync_hypa: failed to publish realtime popup")


# --------------------------------------------------------------------------- #
# Lookups & small helpers
# --------------------------------------------------------------------------- #
def _prepaid_sources(si_name: str) -> list[dict]:
	"""Distinct prepaid Wave Sales Orders behind this SI's items, with their iPay + hold fields."""
	rows = frappe.get_all("Sales Invoice Item", filters={"parent": si_name}, fields=["sales_order"], distinct=True)
	sources: list[dict] = []
	seen: set[str] = set()
	for row in rows:
		so = (row.get("sales_order") or "").strip()
		if not so or so in seen:
			continue
		seen.add(so)
		so_row = frappe.db.get_value(
			"Sales Order", so,
			[
				"wave_payment_classification", "wave_ipay_transaction_code", "wave_ipay_paid_at",
				"wave_payment_type", "wave_payment_hold", "wave_additional_payment_hold",
				"wave_order_id", "wave_friendly_id",
			],
			as_dict=True,
		)
		if so_row and (so_row.wave_payment_classification or "") == "prepaid":
			sources.append({
				"so": so,
				"transaction_code": so_row.wave_ipay_transaction_code,
				"paid_at": so_row.wave_ipay_paid_at,
				"payment_type": so_row.wave_payment_type,
				"wave_payment_hold": so_row.wave_payment_hold,
				"wave_additional_payment_hold": so_row.wave_additional_payment_hold,
				"wave_order_id": so_row.wave_order_id,
				"wave_friendly_id": so_row.wave_friendly_id,
			})
	return sources


def _order_anchors(txn: str, wave_order_id, wave_friendly_id) -> list[list]:
	"""OR-filter anchors that identify a PE belonging to this order: txn code / wave ids."""
	anchors = [["reference_no", "=", txn]]
	if wave_order_id:
		anchors.append(["wave_order_id", "=", wave_order_id])
	if wave_friendly_id:
		anchors.append(["wave_friendly_id", "=", wave_friendly_id])
	return anchors


def _find_pe_for_order(txn, wave_order_id, wave_friendly_id):
	"""Return (name, docstatus) of the oldest non-cancelled PE belonging to this order, or None."""
	rows = frappe.get_all(
		"Payment Entry",
		filters={"docstatus": ("!=", 2)},
		or_filters=_order_anchors(txn, wave_order_id, wave_friendly_id),
		fields=["name", "docstatus"],
		order_by="creation asc",
		limit=1,
	)
	return (rows[0].name, rows[0].docstatus) if rows else None


def _other_live_pes_for_order(txn, wave_order_id, wave_friendly_id, exclude: str) -> list[str]:
	"""Names of OTHER non-cancelled PEs belonging to this order (concurrent-creator detection)."""
	return frappe.get_all(
		"Payment Entry",
		filters={"docstatus": ("!=", 2), "name": ("!=", exclude)},
		or_filters=_order_anchors(txn, wave_order_id, wave_friendly_id),
		pluck="name",
	)


def _pe_references_si(pe_name: str, si_name: str) -> bool:
	"""True when a (submitted) Payment Entry already references this Sales Invoice."""
	return bool(frappe.db.exists(
		"Payment Entry Reference",
		{"parent": pe_name, "reference_doctype": "Sales Invoice", "reference_name": si_name},
	))


def _doc_references_si(pe, si_name: str) -> bool:
	"""True when the in-memory PE doc already has a reference row for this SI."""
	return any(
		r.reference_doctype == "Sales Invoice" and r.reference_name == si_name
		for r in (pe.references or [])
	)


def _bank_account_for(mode_of_payment, company) -> str | None:
	"""Company default bank/cash account for a Mode of Payment, or None (get_bank_cash_account throws when unset)."""
	try:
		return (get_bank_cash_account(mode_of_payment, company) or {}).get("account")
	except Exception:
		return None


def _reference_date(paid_at):
	"""Use iPay's paid_at date for the PE reference date, falling back to today."""
	if paid_at:
		try:
			return getdate(paid_at)
		except Exception:
			pass
	return nowdate()


def _lock_name(transaction_code: str) -> str:
	"""Filesystem-safe per-transaction lock name."""
	safe = "".join(c for c in transaction_code if c.isalnum())
	return f"wave-ipay-pe-{safe}"


def _enabled_settings():
	"""Wave Settings when both the master switch and the auto-create flag are on, else None."""
	if not is_wave_integration_enabled():
		return None
	settings = frappe.get_cached_doc("Wave Settings")
	return settings if settings.get("ipay_auto_create_payment_entry") else None


def _flag(si_name, settings, correlation_id, step, reason, request_body=None) -> None:
	"""Flag the SI for accounting follow-up and log the reason."""
	payment_review_flag.flag("Sales Invoice", si_name, reason, settings=settings, correlation_id=correlation_id)
	log_step(
		correlation_id=correlation_id, step=step, level="Warning",
		doc_type="Sales Invoice", linked_doctype="Sales Invoice", linked_docname=si_name,
		request_body=request_body, error_message=reason,
	)


def _log_unexpected(doctype, name, correlation_id, exc, where) -> None:
	"""Audit a swallowed worker exception."""
	log_step(
		correlation_id=correlation_id, step=STEP_UNEXPECTED_ERROR, level="Error",
		doc_type=doctype, linked_doctype=doctype, linked_docname=name,
		error_message=f"unexpected exception in {where}: {exc}", stack_trace=frappe.get_traceback(),
	)
