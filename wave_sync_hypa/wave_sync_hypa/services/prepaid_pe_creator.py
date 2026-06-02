"""On a prepaid Sales Invoice submit, ensure a Payment Entry exists for the
iPay payment — our app is the source of truth.

PR #129 stamps `wave_ipay_transaction_code` on prepaid Sales Orders. n8n
still creates unallocated draft iPay PEs (`reference_no` = the transaction
code, party = the "Ipay Unallocated" placeholder, `references = []`). So we
search for a Payment Entry whose `reference_no` equals the transaction code:

  * DRAFT match (n8n's): overwrite party + mode_of_payment + reference_date
    with our details, ATTACH this Sales Invoice, then submit — our data wins.
  * SUBMITTED match already referencing this SI: idempotent, leave it.
  * SUBMITTED match NOT referencing this SI: can't mutate a submitted doc ->
    flag the SI for accounting.
  * no match: build one via ERPNext get_payment_entry and submit.

Submitting flows through the existing chain (validate_payment_before_submit
-> on_payment_entry_submit -> paymentStatus push, PR #121). Any submit-time
block (amount / MOP rules) leaves the PE as a draft and flags the SI.

Enqueued after the SI submit commits; never raises.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt, getdate, nowdate

from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

from wave_sync_hypa.wave_sync_hypa.services import ipay_payment_sync, payment_review_flag
from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step
from wave_sync_hypa.wave_sync_hypa.services.master_switch import is_wave_integration_enabled

WORKER_DOTTED_PATH = (
	"wave_sync_hypa.wave_sync_hypa.services.prepaid_pe_creator.create_payment_entry_worker"
)

STEP_ENQUEUED = "prepaid_pe_create_enqueued"
STEP_SKIPPED_NOT_PREPAID = "prepaid_pe_create_skipped_not_prepaid"
STEP_MULTI_SOURCE = "prepaid_pe_create_multi_source_flagged"
STEP_NO_TXN_CODE = "prepaid_pe_create_no_transaction_code"
STEP_UPDATED_DRAFT = "prepaid_pe_create_updated_existing_draft"
STEP_ALREADY_SETTLED = "prepaid_pe_create_already_settled"
STEP_BLOCKED_SUBMITTED_PE = "prepaid_pe_create_blocked_submitted_pe"
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
		["docstatus", "is_return", "customer", "outstanding_amount"],
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
		_flag(si_name, settings, correlation_id, STEP_MULTI_SOURCE,
			"Sales Invoice draws from multiple prepaid Wave orders; create the Payment Entry(s) manually.")
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
			_flag(si_name, settings, correlation_id, STEP_BLOCKED_SUBMITTED_PE,
				f"Submitted Payment Entry {name} already carries iPay transaction {txn} but does not "
				"reference this invoice; reconcile manually (a submitted PE cannot be modified).")
		return

	_create_and_submit(si_name, si, src, settings, correlation_id, txn)


def _create_and_submit(si_name, si, src, settings, correlation_id, txn) -> None:
	"""Build a fresh PE from the SI via ERPNext's helper, stamp our fields, submit."""
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
	_submit(pe, si_name, settings, correlation_id, txn)


def _update_and_submit_draft(pe_name, si_name, si, src, settings, correlation_id, txn) -> None:
	"""Overwrite an existing draft (n8n unallocated) PE with our details + attach this SI, submit."""
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


def _prepaid_sources(si_name: str) -> list[dict]:
	"""Distinct prepaid Wave Sales Orders behind this SI's items, with their iPay fields."""
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
			["wave_payment_classification", "wave_ipay_transaction_code", "wave_ipay_paid_at", "wave_payment_type"],
			as_dict=True,
		)
		if so_row and (so_row.wave_payment_classification or "") == "prepaid":
			sources.append({
				"so": so,
				"transaction_code": so_row.wave_ipay_transaction_code,
				"paid_at": so_row.wave_ipay_paid_at,
				"payment_type": so_row.wave_payment_type,
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


def _flag(si_name, settings, correlation_id, step, reason) -> None:
	"""Flag the SI for accounting follow-up and log the reason."""
	payment_review_flag.flag("Sales Invoice", si_name, reason, settings=settings, correlation_id=correlation_id)
	log_step(
		correlation_id=correlation_id, step=step, level="Warning",
		doc_type="Sales Invoice", linked_doctype="Sales Invoice", linked_docname=si_name,
		error_message=reason,
	)
