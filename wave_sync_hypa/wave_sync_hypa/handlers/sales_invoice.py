"""Sales Invoice hooks: stamp wave_order_id and push UNDER_DELIVERY on submit.

Wired in hooks.py:

  Sales Invoice.validate    -> stamp_wave_order_id
  Sales Invoice.on_submit   -> on_sales_invoice_submit

Like the DN handler, the SI's `wave_order_id` Custom Field is no_copy=1,
so the standard "Make Sales Invoice from Sales Order / Delivery Note"
mapper drops the value. The validate hook re-derives it from the source
doc graph at insert / save time.

The submit hook fans the status push out to every distinct
`wave_order_id` the invoice touches. A single SI can bridge multiple
Wave-sourced SOs (when the operator combines lines from several SOs into
one invoice), so we collect, dedupe, and dispatch once per leg.

Credit notes (`is_return=1`) are deliberately skipped here. PR-E will
add the full-value-Credit-Note -> CANCELLED branch in this same module;
landing it as a separate PR keeps the diff small and the test surface
focused. Until then, return invoices are a no-op on the Wave side.
"""

from __future__ import annotations

import frappe

from wave_sync_hypa.wave_sync_hypa.handlers import order_status
from wave_sync_hypa.wave_sync_hypa.services import credit_note_classifier, prepaid_pe_creator
from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step
from wave_sync_hypa.wave_sync_hypa.services.master_switch import skip_if_disabled

STEP_STAMP_MULTI_SOURCE = "sales_invoice_wave_order_id_multi_source"
STEP_SKIPPED_PARTIAL_RETURN = "sales_invoice_status_push_skipped_partial_return"
STEP_FULL_RETURN_DETECTED = "sales_invoice_full_value_credit_note_detected"

# Event tag used on log rows for credit-note-driven dispatches. Distinct
# from "submit" so audit dashboards can separate "regular SI submit" from
# "credit note submit" without having to cross-reference the SI itself.
EVENT_CREDIT_NOTE_SUBMIT = "credit_note_submit"


def stamp_wave_order_id(doc, method=None) -> None:
	"""Validate hook: copy the source SO/DN's wave_order_id onto the SI.

	Idempotent. When the SI's items[] reach multiple distinct Wave-sourced
	SOs we stamp the first id (so the SI form is filterable by Wave order)
	and emit a Warning enumerating all of them. The submit-time push will
	fan out to each.
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
				f"Sales Invoice draws items from {len(wave_ids)} distinct Wave-sourced "
				"Sales Orders. Stamping the first on wave_order_id; status push will fan out "
				"to all of them at submit."
			),
		)


def on_sales_invoice_submit(doc, method=None) -> None:
	"""Submit hook: route to UNDER_DELIVERY (regular), CANCELLED (full credit), or skip (partial).

	Three branches:
	  * is_return=1 AND credit_note_classifier classifies as full-value:
	    push CANCELLED to every linked Wave order, tagged with the
	    `credit_note_submit` event so the audit trail distinguishes this
	    from a regular SI submit.
	  * is_return=1 AND not full-value (or unclassifiable):
	    log STEP_SKIPPED_PARTIAL_RETURN. The Wave order stays at
	    UNDER_DELIVERY; Phase 8 Payment Entry reconciliation will model
	    the partial-return -> PAYMENT_PENDING transition.
	  * is_return=0 (regular invoice):
	    push UNDER_DELIVERY via the rule resolver, fan out to every
	    linked wave_order_id.
	"""
	wave_ids = _collect_distinct_wave_order_ids(doc)
	if not wave_ids and doc.get("wave_order_id"):
		wave_ids = [doc.wave_order_id]

	if doc.get("is_return"):
		_handle_return(doc, wave_ids)
		return

	order_status.dispatch_with_wave_order_ids(doc, "submit", wave_ids)

	# Prepaid orders: ensure the iPay Payment Entry exists (find-update-attach
	# / create). Only worth queuing for Wave-sourced SIs; the worker does the
	# precise prepaid + single-source check.
	if wave_ids:
		_maybe_create_prepaid_payment_entry(doc)


def _maybe_create_prepaid_payment_entry(doc) -> None:
	"""Enqueue the prepaid PE create/attach when the feature flag + master switch allow it."""
	settings = frappe.get_cached_doc("Wave Settings")
	if not settings.get("ipay_auto_create_payment_entry"):
		return
	correlation_id = new_correlation_id()
	if skip_if_disabled(
		correlation_id,
		doc_type="Sales Invoice",
		action="prepaid_payment_entry",
		linked_doctype="Sales Invoice",
		linked_docname=doc.name,
	):
		return
	prepaid_pe_creator.enqueue_payment_entry_creation(doc, correlation_id)


def _handle_return(doc, wave_ids: list[str]) -> None:
	"""Classify a return Sales Invoice and either dispatch CANCELLED or log a partial-return skip."""
	if credit_note_classifier.is_full_value_credit_note(doc):
		log_step(
			correlation_id=new_correlation_id(),
			step=STEP_FULL_RETURN_DETECTED,
			level="Info",
			doc_type=doc.doctype,
			linked_doctype=doc.doctype,
			linked_docname=doc.name,
			wave_id=doc.get("wave_order_id") or None,
			request_body={
				"return_against": doc.get("return_against"),
				"credit_grand_total": float(doc.get("grand_total") or 0),
				"wave_order_ids": wave_ids,
			},
			error_message=(
				"Full-value Credit Note detected; pushing CANCELLED to every linked Wave order."
			),
		)
		order_status.dispatch_with_wave_order_ids(
			doc,
			EVENT_CREDIT_NOTE_SUBMIT,
			wave_ids,
			forced_payload={"status": "CANCELLED"},
		)
		return

	# Partial return / unclassifiable return: log + skip. Operator-facing
	# message names the comparison so triage doesn't need to dig through
	# code to understand why we didn't push.
	log_step(
		correlation_id=new_correlation_id(),
		step=STEP_SKIPPED_PARTIAL_RETURN,
		level="Info",
		doc_type=doc.doctype,
		linked_doctype=doc.doctype,
		linked_docname=doc.name,
		wave_id=doc.get("wave_order_id") or None,
		request_body={
			"return_against": doc.get("return_against"),
			"credit_grand_total": float(doc.get("grand_total") or 0),
		},
		error_message=(
			"Credit Note is a partial return (or could not be classified). "
			"No Wave status push; the Wave order stays at UNDER_DELIVERY. "
			"Partial-return -> PAYMENT_PENDING will be modelled by Phase 8 Payment Entry reconciliation."
		),
	)


def _collect_distinct_wave_order_ids(doc) -> list[str]:
	"""Return the unique Wave order ids reachable from this SI's items.

	Two paths walked in order — first hit per item wins:
	  1. item.sales_order -> Sales Order.wave_order_id  (SI made from SO)
	  2. item.delivery_note -> Delivery Note.wave_order_id  (SI made from DN)

	Iteration order is items order so that for a multi-source invoice the
	"first stamped id" is deterministic and reflects the operator's row
	ordering.
	"""
	seen: set[str] = set()
	out: list[str] = []
	for item in doc.get("items") or []:
		wave_order_id = _resolve_item_wave_order_id(item)
		if wave_order_id and wave_order_id not in seen:
			seen.add(wave_order_id)
			out.append(wave_order_id)
	return out


def _resolve_item_wave_order_id(item) -> str:
	"""Return the wave_order_id reachable from a single SI item, or '' if none."""
	so_name = (item.get("sales_order") or "").strip()
	if so_name:
		so_wave_id = (frappe.db.get_value("Sales Order", so_name, "wave_order_id") or "").strip()
		if so_wave_id:
			return so_wave_id
	dn_name = (item.get("delivery_note") or "").strip()
	if dn_name:
		dn_wave_id = (frappe.db.get_value("Delivery Note", dn_name, "wave_order_id") or "").strip()
		if dn_wave_id:
			return dn_wave_id
	return ""
