"""Delivery Note hooks: stamp wave_order_id and push the status transition to Wave.

Wired in hooks.py:

  Delivery Note.validate    -> stamp_wave_order_id
  Delivery Note.on_submit   -> on_delivery_note_submit

The stamping pass walks `delivery_note_items[].against_sales_order` to find
the source Sales Orders, looks up their `wave_order_id`, and persists the
first match on `Delivery Note.wave_order_id` so the DN is filterable in the
Desk and the field is one-shot retrievable for downstream code (Sales
Invoice creation, audit queries, etc.). It runs at validate time because
the standard "Make Delivery Note from Sales Order" mapper does NOT carry
wave_order_id forward — the field is `no_copy=1` on Sales Order — so we
re-derive the link at insert time.

The submit pass is intentionally fan-out aware: a single DN can draw items
from two Wave-sourced SOs, in which case Wave needs to know the new
status for both Wave order ids. We collect the distinct list of Wave order
ids and dispatch one push per leg.

Cancellation is a deliberate no-op. Operators who cancel a DN are usually
correcting an internal data error, not signalling a customer-facing event,
so we do not push to Wave on DN cancel. Cancellation as a customer-facing
event is modelled instead via a full-value Credit Note on the Sales
Invoice (handled by the SI handler).
"""

from __future__ import annotations

import frappe

from wave_sync_hypa.wave_sync_hypa.handlers import order_status
from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step

STEP_STAMP = "delivery_note_wave_order_id_stamped"
STEP_STAMP_MULTI_SOURCE = "delivery_note_wave_order_id_multi_source"
STEP_STAMP_NO_WAVE_SOURCE = "delivery_note_wave_order_id_no_wave_source"


def stamp_wave_order_id(doc, method=None) -> None:
	"""Validate hook: copy the source SO's wave_order_id onto the DN.

	Idempotent: if doc.wave_order_id is already populated (e.g. a previous
	validate pass on the same in-memory doc, or an explicit operator entry
	on a corner-case manually-built DN), we don't overwrite it.

	When the DN draws from multiple distinct Wave-sourced SOs, we stamp the
	first id on the field (so the form stays searchable / filterable) and
	emit a Warning row naming every distinct id we found. The status push
	pipeline still fans out to all of them at submit time.
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
				f"Delivery Note draws items from {len(wave_ids)} distinct Wave-sourced "
				"Sales Orders. Stamping the first on wave_order_id; status push will fan out "
				"to all of them at submit."
			),
		)


def on_delivery_note_submit(doc, method=None) -> None:
	"""Submit hook: dispatch INVOICING (or whatever the rule says) to every linked Wave order."""
	wave_ids = _collect_distinct_wave_order_ids(doc)
	# Fall back to the stamped value if items don't surface a Wave-sourced SO
	# (e.g. an operator manually populated DN.wave_order_id without an items link).
	if not wave_ids and doc.get("wave_order_id"):
		wave_ids = [doc.wave_order_id]
	order_status.dispatch_with_wave_order_ids(doc, "submit", wave_ids)


def _collect_distinct_wave_order_ids(doc) -> list[str]:
	"""Return the unique Wave order ids reachable from this DN's items, in items order.

	Walks each item's `against_sales_order` (the standard ERPNext linkage
	from DN to SO) and dereferences `Sales Order.wave_order_id`. Items that
	don't link back to a Wave-sourced SO are silently skipped — a single
	mixed DN that combines Wave and non-Wave lines is a legitimate flow.
	"""
	seen: set[str] = set()
	out: list[str] = []
	for item in doc.get("items") or []:
		so_name = (item.get("against_sales_order") or "").strip()
		if not so_name:
			continue
		wave_order_id = (frappe.db.get_value("Sales Order", so_name, "wave_order_id") or "").strip()
		if wave_order_id and wave_order_id not in seen:
			seen.add(wave_order_id)
			out.append(wave_order_id)
	return out
