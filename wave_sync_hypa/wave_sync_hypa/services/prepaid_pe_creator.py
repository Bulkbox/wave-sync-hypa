"""On a prepaid Sales Invoice submit, ensure a Payment Entry exists for the
iPay payment — our app is the source of truth.

PR #129 stamps `wave_ipay_transaction_code` on prepaid Sales Orders. n8n
still creates unallocated draft iPay PEs (`reference_no` = the transaction
code, party = the "Ipay Unallocated" placeholder, `references = []`). So we
search for a Payment Entry whose `reference_no` equals the transaction code:

  * DRAFT match (n8n's): overwrite party + mode_of_payment + reference_date
    with our details, ATTACH this Sales Invoice — our data wins.
  * SUBMITTED match already referencing this SI: idempotent, leave it.
  * SUBMITTED match NOT referencing this SI: a submitted PE cannot be
    modified, and a second PE for the same iPay transaction would be a
    double-settlement. We never touch it; instead we raise an integrity
    alarm (comment on the PE + assign it + flag the SI + notify the
    invoice's creator) for manual reconciliation.
  * no match: build one via ERPNext get_payment_entry.

Integrity guards:
  * the find-or-create runs under a per-transaction file lock so two of our
    own workers can't both create a PE for one iPay transaction; and
  * because `reference_no` is a generic, non-unique ERPNext field (no DB
    constraint is possible), after creating we re-check for a second live PE
    sharing the code — catching a concurrent external creator (n8n) — and
    alarm instead of submitting.

A created/adopted PE is submitted ONLY when its amount reconciles
(invoice grand_total == the Wave-authorised hold within tolerance);
otherwise it is left as a DRAFT and the invoice is flagged for manual
review. Submitting flows through the existing chain (validate ->
on_payment_entry_submit -> paymentStatus push, PR #121).

Enqueued after the SI submit commits; never raises.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt, getdate, nowdate
from frappe.utils.file_lock import LockTimeoutError
from frappe.utils.synchronization import filelock

from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

from wave_sync_hypa.wave_sync_hypa.services import ipay_payment_sync, payment_review_flag
from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step
from wave_sync_hypa.wave_sync_hypa.services.master_switch import is_wave_integration_enabled
from wave_sync_hypa.wave_sync_hypa.services.payment_status_resolver import FULL_PAYMENT_TOLERANCE

WORKER_DOTTED_PATH = (
	"wave_sync_hypa.wave_sync_hypa.services.prepaid_pe_creator.create_payment_entry_worker"
)

# Per-transaction lock so two of our own workers can't both create a PE for
# one iPay transaction. Held only for the brief find-or-create window.
LOCK_TIMEOUT_SECONDS = 30

# Marker on PE timeline Comments we add, so the conflict note is idempotent
# (we never spam the same PE with duplicate alarms on a retry).
_PE_CONFLICT_MARKER = "Wave Sync — iPay transaction conflict"

STEP_ENQUEUED = "prepaid_pe_create_enqueued"
STEP_SKIPPED_NOT_PREPAID = "prepaid_pe_create_skipped_not_prepaid"
STEP_MULTI_SOURCE = "prepaid_pe_create_multi_source_flagged"
STEP_NO_TXN_CODE = "prepaid_pe_create_no_transaction_code"
STEP_UPDATED_DRAFT = "prepaid_pe_create_updated_existing_draft"
STEP_ALREADY_SETTLED = "prepaid_pe_create_already_settled"
STEP_BLOCKED_SUBMITTED_PE = "prepaid_pe_create_blocked_submitted_pe"
STEP_DUPLICATE_DETECTED = "prepaid_pe_create_duplicate_detected"
STEP_AMOUNT_MISMATCH = "prepaid_pe_create_amount_mismatch_draft_left"
STEP_LOCK_TIMEOUT = "prepaid_pe_create_lock_timeout"
STEP_CREATED = "prepaid_pe_create_created"
STEP_SUBMIT_BLOCKED = "prepaid_pe_create_submit_blocked"
STEP_SUBMITTED = "prepaid_pe_create_submitted"
STEP_UNEXPECTED_ERROR = "prepaid_pe_create_unexpected_error"


def enqueue_payment_entry_creation(si_doc, correlation_id: str) -> None:
	"""Queue the async PE create/attach for a submitted prepaid SI (after_commit)."""
	frappe.enqueue(
		WORKER_DOTTED_PATH,
		queue="default",
		enqueue_after_commit=True,
		job_name=f"prepaid_pe:{si_doc.name}",
		sales_invoice=si_doc.name,
		correlation_id=correlation_id,
	)
	log_step(
		correlation_id=correlation_id,
		step=STEP_ENQUEUED,
		level="Info",
		doc_type="Sales Invoice",
		linked_doctype="Sales Invoice",
		linked_docname=si_doc.name,
	)


def create_payment_entry_worker(*, sales_invoice: str, correlation_id: str) -> None:
	"""Async entry: master-switch + auto-create-flag backstop, then ensure the PE. Never raises."""
	try:
		if not is_wave_integration_enabled():
			return
		settings = frappe.get_cached_doc("Wave Settings")
		if not settings.get("ipay_auto_create_payment_entry"):
			return
		_ensure_payment_entry(sales_invoice, settings, correlation_id)
	except Exception as exc:
		log_step(
			correlation_id=correlation_id,
			step=STEP_UNEXPECTED_ERROR,
			level="Error",
			doc_type="Sales Invoice",
			linked_doctype="Sales Invoice",
			linked_docname=sales_invoice,
			error_message=f"unexpected exception in create_payment_entry_worker: {exc}",
			stack_trace=frappe.get_traceback(),
		)


def _ensure_payment_entry(si_name: str, settings, correlation_id: str) -> None:
	"""Find-update-attach / create the Payment Entry for a prepaid SI. Our data wins."""
	si = frappe.db.get_value(
		"Sales Invoice",
		si_name,
		["docstatus", "is_return", "customer", "outstanding_amount", "grand_total", "owner"],
		as_dict=True,
	)
	if not si or si.docstatus != 1 or si.is_return:
		return

	sources = _prepaid_sources(si_name)
	if not sources:
		log_step(
			correlation_id=correlation_id, step=STEP_SKIPPED_NOT_PREPAID, level="Info",
			doc_type="Sales Invoice", linked_doctype="Sales Invoice", linked_docname=si_name,
		)
		return
	if len(sources) > 1:
		_flag(
			si_name, settings, correlation_id, STEP_MULTI_SOURCE,
			"Sales Invoice draws from multiple prepaid Wave orders; create the Payment Entry(s) manually.",
			request_body={"sales_orders": [s["so"] for s in sources]},
		)
		return

	src = sources[0]
	txn = (src.get("transaction_code") or "").strip()
	if not txn:
		# PR-129 may not have stamped yet (e.g. iPay was slow at intake) — try once more.
		ipay_payment_sync.fetch_and_stamp(src["so"], correlation_id, settings=settings)
		txn = (frappe.db.get_value("Sales Order", src["so"], "wave_ipay_transaction_code") or "").strip()
	if not txn:
		_flag(si_name, settings, correlation_id, STEP_NO_TXN_CODE,
			"No verified iPay payment (no transaction code) for this prepaid order; cannot create a Payment Entry.")
		return

	# Serialise our own concurrent workers on this iPay transaction. reference_no
	# is a generic non-unique field so a DB constraint isn't an option; the lock
	# closes our self-race, and _create_and_submit's post-insert re-check closes
	# the external (n8n) race the lock can't see.
	try:
		with filelock(_lock_name(txn), timeout=LOCK_TIMEOUT_SECONDS):
			_settle_under_lock(si_name, si, src, settings, correlation_id, txn)
	except LockTimeoutError:
		_flag(si_name, settings, correlation_id, STEP_LOCK_TIMEOUT,
			f"Another process is already settling iPay transaction {txn}; Payment Entry creation "
			"was skipped to avoid a duplicate. Re-check shortly.")


def _settle_under_lock(si_name, si, src, settings, correlation_id, txn) -> None:
	"""Find-or-create branch, run while holding the per-transaction lock."""
	existing = _find_pe_by_reference(txn)
	if existing:
		name, docstatus = existing
		if docstatus == 0:
			_update_and_submit_draft(name, si_name, si, src, settings, correlation_id, txn)
		elif _pe_references_si(name, si_name):
			log_step(
				correlation_id=correlation_id, step=STEP_ALREADY_SETTLED, level="Info",
				doc_type="Sales Invoice", linked_doctype="Sales Invoice", linked_docname=si_name,
				request_body={"payment_entry": name, "transaction_code": txn},
			)
		else:
			_raise_submitted_conflict_alarm(si_name, si, name, txn, settings, correlation_id)
		return

	_create_and_submit(si_name, si, src, settings, correlation_id, txn)


def _create_and_submit(si_name, si, src, settings, correlation_id, txn) -> None:
	"""Build a fresh PE from the SI via ERPNext's helper, stamp our fields, submit if reconciled."""
	try:
		pe = get_payment_entry("Sales Invoice", si_name, reference_date=_reference_date(src))
		pe.reference_no = txn
		mop = _mode_of_payment_for(settings, src.get("payment_type"))
		if mop:
			pe.mode_of_payment = mop
		pe.insert(ignore_permissions=True)
		log_step(
			correlation_id=correlation_id, step=STEP_CREATED, level="Info",
			doc_type="Sales Invoice", linked_doctype="Payment Entry", linked_docname=pe.name,
			request_body={"sales_invoice": si_name, "transaction_code": txn},
		)
	except Exception as exc:
		_flag(si_name, settings, correlation_id, STEP_SUBMIT_BLOCKED,
			f"Could not build the Payment Entry for iPay transaction {txn}: {exc}")
		return

	# A second live PE for this reference_no means an external creator (n8n) raced
	# us inside our lock window. Never submit a duplicate — alarm and leave both
	# as drafts for manual reconciliation.
	others = _other_live_pes_by_reference(txn, exclude=pe.name)
	if others:
		_raise_duplicate_alarm(si_name, si, pe.name, others, txn, settings, correlation_id)
		return

	_submit_if_reconciled(pe, si_name, si, src, settings, correlation_id, txn)


def _update_and_submit_draft(pe_name, si_name, si, src, settings, correlation_id, txn) -> None:
	"""Overwrite an existing draft (n8n unallocated) PE with our details + attach this SI, submit if reconciled."""
	try:
		pe = frappe.get_doc("Payment Entry", pe_name)
		pe.party_type = "Customer"
		pe.party = si.customer
		# n8n created this PE against the "Ipay Unallocated" placeholder customer;
		# the receivable account is cached in __init__, so clear it to force
		# set_missing_values() to recompute paid_from for the real customer.
		pe.party_account = None
		mop = _mode_of_payment_for(settings, src.get("payment_type"))
		if mop:
			pe.mode_of_payment = mop
		pe.reference_date = _reference_date(src)
		if not _doc_references_si(pe, si_name):
			pe.append("references", {
				"reference_doctype": "Sales Invoice",
				"reference_name": si_name,
				"allocated_amount": flt(si.outstanding_amount),
			})
		pe.set_missing_values()
		pe.save(ignore_permissions=True)
		log_step(
			correlation_id=correlation_id, step=STEP_UPDATED_DRAFT, level="Info",
			doc_type="Sales Invoice", linked_doctype="Payment Entry", linked_docname=pe.name,
			request_body={"sales_invoice": si_name, "transaction_code": txn},
		)
	except Exception as exc:
		_flag(si_name, settings, correlation_id, STEP_SUBMIT_BLOCKED,
			f"Could not update the existing draft Payment Entry {pe_name} for iPay transaction {txn}: {exc}")
		return
	_submit_if_reconciled(pe, si_name, si, src, settings, correlation_id, txn)


def _submit_if_reconciled(pe, si_name, si, src, settings, correlation_id, txn) -> None:
	"""Submit only when the invoice total matches the Wave-authorised hold; else leave a draft + flag.

	A missing/zero hold is treated as "cannot verify" (not "no check needed"):
	a prepaid order should carry an authorised amount, so its absence is itself
	a reason to hold the Payment Entry as a draft for manual review.
	"""
	expected = flt(src.get("wave_payment_hold")) + flt(src.get("wave_additional_payment_hold"))
	grand_total = flt(si.grand_total)
	if not expected:
		_flag(
			si_name, settings, correlation_id, STEP_AMOUNT_MISMATCH,
			f"Payment Entry {pe.name} was created as a DRAFT but NOT submitted: no Wave-authorised "
			f"amount (payment hold) is recorded for this prepaid order, so the invoice total "
			f"{grand_total:.2f} could not be verified. Verify the iPay payment and submit manually.",
			request_body={"payment_entry": pe.name, "grand_total": grand_total, "expected": expected},
		)
		return
	if abs(grand_total - expected) >= FULL_PAYMENT_TOLERANCE:
		_flag(
			si_name, settings, correlation_id, STEP_AMOUNT_MISMATCH,
			f"Payment Entry {pe.name} was created as a DRAFT but NOT submitted: invoice total "
			f"{grand_total:.2f} does not match the iPay/Wave authorised amount {expected:.2f} "
			f"(difference {grand_total - expected:+.2f}). Review the amounts and submit the "
			"Payment Entry manually.",
			request_body={"payment_entry": pe.name, "grand_total": grand_total, "expected": expected},
		)
		return
	_submit(pe, si_name, settings, correlation_id, txn)


def _submit(pe, si_name, settings, correlation_id, txn) -> None:
	"""Submit the PE; clear the SI review flag on success, flag it on a validator block."""
	try:
		pe.submit()
	except Exception as exc:
		_flag(si_name, settings, correlation_id, STEP_SUBMIT_BLOCKED,
			f"Payment Entry {pe.name} for iPay transaction {txn} could not be submitted: {exc}")
		return
	payment_review_flag.clear("Sales Invoice", si_name, settings=settings, correlation_id=correlation_id)
	log_step(
		correlation_id=correlation_id, step=STEP_SUBMITTED, level="Success",
		doc_type="Sales Invoice", linked_doctype="Payment Entry", linked_docname=pe.name,
		request_body={"sales_invoice": si_name, "transaction_code": txn},
	)


def _raise_submitted_conflict_alarm(si_name, si, pe_name, txn, settings, correlation_id) -> None:
	"""A submitted PE owns this txn but not this SI: never touch it; alarm loudly for manual reconciliation."""
	reason = (
		f"iPay transaction {txn} is already settled by submitted Payment Entry {pe_name}, which does "
		f"NOT reference this invoice. A submitted Payment Entry cannot be modified and a second one "
		"would double-settle the payment. Verify the customer was not double-charged and reconcile manually."
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


def _prepaid_sources(si_name: str) -> list[dict]:
	"""Distinct prepaid Wave Sales Orders behind this SI's items, with their iPay + hold fields."""
	rows = frappe.get_all(
		"Sales Invoice Item",
		filters={"parent": si_name},
		fields=["sales_order"],
		distinct=True,
	)
	sources: list[dict] = []
	seen: set[str] = set()
	for row in rows:
		so = (row.get("sales_order") or "").strip()
		if not so or so in seen:
			continue
		seen.add(so)
		so_row = frappe.db.get_value(
			"Sales Order",
			so,
			[
				"wave_payment_classification", "wave_ipay_transaction_code", "wave_ipay_paid_at",
				"wave_payment_type", "wave_payment_hold", "wave_additional_payment_hold",
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
			})
	return sources


def _find_pe_by_reference(transaction_code: str):
	"""Return (name, docstatus) of the oldest non-cancelled PE with this reference_no, or None."""
	rows = frappe.get_all(
		"Payment Entry",
		filters={"reference_no": transaction_code, "docstatus": ("!=", 2)},
		fields=["name", "docstatus"],
		order_by="creation asc",
		limit=1,
	)
	return (rows[0].name, rows[0].docstatus) if rows else None


def _other_live_pes_by_reference(transaction_code: str, exclude: str) -> list[str]:
	"""Names of OTHER non-cancelled PEs sharing this reference_no (concurrent-creator detection)."""
	return frappe.get_all(
		"Payment Entry",
		filters={"reference_no": transaction_code, "docstatus": ("!=", 2), "name": ("!=", exclude)},
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


def _mode_of_payment_for(settings, payment_type) -> str | None:
	"""Mode of Payment mapped to this Wave paymentType, or None when unmapped."""
	payment_type = (payment_type or "").strip()
	if not payment_type:
		return None
	for row in settings.get("payment_method_mappings") or []:
		if (row.get("wave_payment_type") or "").strip() == payment_type:
			return (row.get("mode_of_payment") or "").strip() or None
	return None


def _reference_date(src: dict):
	"""Use iPay's paid_at date for the PE reference date, falling back to today."""
	paid_at = src.get("paid_at")
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


def _flag(si_name, settings, correlation_id, step, reason, request_body=None) -> None:
	"""Flag the SI for accounting follow-up and log the reason."""
	payment_review_flag.flag("Sales Invoice", si_name, reason, settings=settings, correlation_id=correlation_id)
	log_step(
		correlation_id=correlation_id, step=step, level="Warning",
		doc_type="Sales Invoice", linked_doctype="Sales Invoice", linked_docname=si_name,
		request_body=request_body, error_message=reason,
	)
