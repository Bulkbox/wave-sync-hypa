"""Payment Entry hooks: stamp wave_order_id and push Wave's paymentStatus on submit.

Wired in hooks.py:

  Payment Entry.validate       -> stamp_wave_order_id
  Payment Entry.before_submit  -> validate_payment_before_submit
  Payment Entry.on_submit      -> on_payment_entry_submit

A Payment Entry can settle multiple Sales Invoices and/or Sales Orders via
its `references` child table. Each row carries (reference_doctype,
reference_name); both Sales Invoice and Sales Order have the wave_order_id
Custom Field, so we walk the references, dereference each one's
wave_order_id, dedupe in encounter order, and dispatch one paymentStatus
push per distinct Wave order that is now fully settled.

PE submit drives Wave's `paymentStatus` field only — NOT the order
lifecycle `status` field. Order lifecycle progression lives elsewhere
(DN submit -> INVOICING, SI submit -> UNDER_DELIVERY, Shipday delivered
-> COMPLETED, full credit note -> CANCELLED). Conflating "paid" with
"delivered" was the old shape; this handler now owns just the payment
half of the contract.

The resolver returns "COMPLETED" for full settlement, None for partial /
zero / unresolvable. None means "nothing to communicate" — Wave's
paymentStatus already starts at PENDING (COD) or COMPLETED (prepaid) at
intake, so re-pushing PENDING is either a no-op or a wrong revert.

Refunds (payment_type=Pay) are skipped at the handler level. Cancellation
of a PE is intentionally NOT wired; an explicit follow-up will design
payment-status retraction if and when that's needed.
"""

from __future__ import annotations

import frappe

from wave_sync_hypa.wave_sync_hypa.handlers import order_status
from wave_sync_hypa.wave_sync_hypa.services import (
	payment_status_pusher,
	payment_status_resolver,
	payment_validator,
	wave_customer_resolver,
)
from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step
from wave_sync_hypa.wave_sync_hypa.services.master_switch import skip_if_disabled
from wave_sync_hypa.wave_sync_hypa.services.pe_references import (
	collect_distinct_wave_order_ids as _collect_distinct_wave_order_ids,
)

STEP_STAMP_MULTI_SOURCE = "payment_entry_wave_order_id_multi_source"
STEP_SKIPPED_PAYMENT_TYPE = "payment_entry_skipped_non_receive_payment_type"
STEP_SKIPPED_PARTIAL_OR_ZERO = "payment_status_push_skipped_partial_or_zero"


def stamp_wave_order_id(doc, method=None) -> None:
	"""validate hook: copy the first reachable wave_order_id onto the PE for filterability.

	Idempotent: skips when wave_order_id is already set. Multi-source PEs
	(references reaching multiple distinct Wave orders) get a Warning row
	enumerating every id so the audit trail is unambiguous.
	"""
	if doc.get("wave_order_id"):
		return
	wave_ids = _collect_distinct_wave_order_ids(doc)
	if not wave_ids:
		return
	doc.wave_order_id = wave_ids[0]
	doc.wave_friendly_id = (
		frappe.db.get_value("Sales Order", {"wave_order_id": wave_ids[0]}, "wave_friendly_id") or ""
	)
	if len(wave_ids) > 1:
		log_step(
			correlation_id=new_correlation_id(),
			step=STEP_STAMP_MULTI_SOURCE,
			level="Warning",
			doc_type=doc.doctype,
			linked_doctype=doc.doctype,
			linked_docname=doc.name or "<new>",
			wave_id=wave_ids[0],
			request_body={"wave_order_ids": wave_ids},
			error_message=(
				f"Payment Entry references {len(wave_ids)} distinct Wave-sourced documents. "
				"Stamping the first on wave_order_id; status push will fan out to all."
			),
		)


def on_payment_entry_submit(doc, method=None) -> None:
	"""on_submit hook: per Wave order, push paymentStatus=COMPLETED only when fully settled.

	Resolver returns "COMPLETED" or None per Wave order. None means partial /
	zero / unresolvable — Wave's existing paymentStatus stays as stamped at
	intake (no push). Fully-settled Wave orders get one PATCH each, fanned
	out via the async pusher.
	"""
	if skip_if_disabled(
		new_correlation_id(),
		doc_type=doc.doctype,
		linked_doctype=doc.doctype,
		linked_docname=doc.name,
	):
		return
	if (doc.get("payment_type") or "").strip() != "Receive":
		log_step(
			correlation_id=new_correlation_id(),
			step=STEP_SKIPPED_PAYMENT_TYPE,
			level="Info",
			doc_type=doc.doctype,
			linked_doctype=doc.doctype,
			linked_docname=doc.name,
			error_message=(
				f"Payment Entry payment_type={doc.get('payment_type')!r}; only 'Receive' "
				"PEs push to Wave (refunds are out of scope)."
			),
		)
		return

	customer = doc.get("party") if (doc.get("party_type") or "") == "Customer" else None
	if wave_customer_resolver.is_erp_to_wave_disabled(customer):
		log_step(
			correlation_id=new_correlation_id(),
			step=wave_customer_resolver.STEP_ERP_TO_WAVE_CUSTOMER_DISABLED,
			level="Info",
			doc_type=doc.doctype,
			linked_doctype=doc.doctype,
			linked_docname=doc.name,
			error_message=f"Customer {customer!r} is ERP → Wave disabled; not pushing payment status.",
		)
		return

	wave_ids = _collect_distinct_wave_order_ids(doc)
	if not wave_ids and doc.get("wave_order_id"):
		wave_ids = [doc.wave_order_id]
	correlation_id = new_correlation_id()
	settled_orders: list[str] = []
	for wave_order_id in wave_ids:
		status = payment_status_resolver.resolve_status_for_wave_order(doc, wave_order_id)
		if status is None:
			log_step(
				correlation_id=correlation_id,
				step=STEP_SKIPPED_PARTIAL_OR_ZERO,
				level="Info",
				doc_type=doc.doctype,
				linked_doctype=doc.doctype,
				linked_docname=doc.name,
				wave_id=wave_order_id,
				error_message=(
					"Settlement is partial / zero / unresolvable; not pushing paymentStatus to Wave."
				),
			)
			continue
		payment_status_pusher.enqueue_payment_status_push(
			doc,
			wave_order_id,
			status,
			correlation_id=correlation_id,
		)
		settled_orders.append(wave_order_id)

	# Non-Shipday completion (issue #118): a fully-settled order optionally
	# advances Wave's order status to COMPLETED on payment. Manager opt-in via
	# Wave Settings; default off so Shipday-tracked deliveries are untouched.
	if settled_orders and _complete_on_payment_entry():
		order_status.dispatch_with_wave_order_ids(
			doc, "payment_entry_completion", settled_orders, forced_payload={"status": "COMPLETED"}
		)


def _complete_on_payment_entry() -> bool:
	"""Manager opt-in: push Wave status=COMPLETED when a fully-settled PE submits."""
	return (
		frappe.get_cached_doc("Wave Settings").get("wave_non_shipday_completion_mode")
		== "On Payment Entry submit"
	)


def validate_payment_before_submit(doc, method=None) -> None:
	"""before_submit hook: delegate to payment_validator; raises ValidationError on hard-block branches.

	Pure thin wrapper kept here so hooks.py points at a handler module like
	the rest of the integration. The actual logic lives in
	services/payment_validator.py.
	"""
	payment_validator.validate_pe_before_submit(doc)


# _collect_distinct_wave_order_ids is imported from services.pe_references above.
# The validator (services.payment_validator) imports the same helper, so both
# code paths share one definition of "which references count as Wave-sourced".
