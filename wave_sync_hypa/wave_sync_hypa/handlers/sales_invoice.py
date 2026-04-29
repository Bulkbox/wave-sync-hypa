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
from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step

STEP_STAMP_MULTI_SOURCE = "sales_invoice_wave_order_id_multi_source"
STEP_SKIPPED_RETURN = "sales_invoice_status_push_skipped_return"


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
	"""Submit hook: dispatch UNDER_DELIVERY to every linked Wave order.

	Return invoices (`is_return=1`) are skipped here — the full-value
	Credit Note -> CANCELLED branch will be added in PR-E. Logging the
	skip explicitly keeps the audit trail self-explanatory ("we saw the
	credit note, we just didn't act on it under the current ruleset").
	"""
	if doc.get("is_return"):
		log_step(
			correlation_id=new_correlation_id(),
			step=STEP_SKIPPED_RETURN,
			level="Info",
			doc_type=doc.doctype,
			linked_doctype=doc.doctype,
			linked_docname=doc.name,
			wave_id=doc.get("wave_order_id") or None,
			error_message=(
				"Sales Invoice is a return (is_return=1); no Wave status push under the "
				"current ruleset. Full-value Credit Note -> CANCELLED ships in a follow-up PR."
			),
		)
		return

	wave_ids = _collect_distinct_wave_order_ids(doc)
	if not wave_ids and doc.get("wave_order_id"):
		wave_ids = [doc.wave_order_id]
	order_status.dispatch_with_wave_order_ids(doc, "submit", wave_ids)


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
