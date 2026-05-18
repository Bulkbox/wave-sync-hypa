"""Delivery Note hooks: stamp wave_order_id, autopopulate fields, push status.

Wired in hooks.py:

  Delivery Note.before_insert -> autopopulate_from_wave_so
  Delivery Note.validate      -> stamp_wave_order_id
  Delivery Note.on_submit     -> on_delivery_note_submit

The autopopulate pass fires once at creation time and pulls delivery_date +
(pickup-only) driver from the first linked Wave Sales Order. It uses
before_insert specifically so it never re-overwrites operator edits after
the initial save. Driver auto-stamping is opt-in via Wave Settings
.wave_pickup_driver — leave blank to skip.

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
STEP_AUTOPOPULATED = "delivery_note_autopopulated_from_wave_so"
STEP_AUTOPOPULATE_HETEROGENEOUS = "delivery_note_autopopulate_heterogeneous_sources"


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


def autopopulate_from_wave_so(doc, method=None) -> None:
	"""before_insert hook: pull delivery_date + (pickup) driver from the linked Wave SO.

	Fires exactly once at DN creation. Five guard clauses keep the path short
	and the mutating block obvious:

	  1. No Wave-sourced linked SO -> no-op.
	  2. Read first Wave SO; if missing -> no-op (defensive).
	  3. Copy delivery_date (the site-managed custom field on Delivery Note).
	  4. If wave_delivery_type != 'Pickup' -> stop here, leave driver for operator.
	  5. If doc.driver already set OR no pickup_driver configured -> stop, respect existing state.
	  6. Stamp doc.driver = settings.wave_pickup_driver.

	When the DN draws from multiple Wave-sourced SOs with conflicting
	delivery_date or wave_delivery_type, the first SO's values win and a
	Warning row names the divergence for operator review.
	"""
	wave_order_ids = _collect_distinct_wave_order_ids(doc)
	if not wave_order_ids:
		return
	primary = _read_wave_so(wave_order_ids[0])
	if not primary:
		return

	_maybe_log_heterogeneous_wave_sources(doc, wave_order_ids, primary)

	autopopulated: dict[str, str] = {}
	if primary.get("delivery_date"):
		doc.delivery_date = primary.get("delivery_date")
		autopopulated["delivery_date"] = str(primary.get("delivery_date"))

	delivery_type = (primary.get("wave_delivery_type") or "").strip()
	if delivery_type == "Pickup" and not (doc.driver or ""):
		settings = frappe.get_cached_doc("Wave Settings")
		pickup_driver = (settings.get("wave_pickup_driver") or "").strip()
		if pickup_driver:
			doc.driver = pickup_driver
			autopopulated["driver"] = pickup_driver

	if autopopulated:
		_log_autopopulated(doc, primary, autopopulated)


def _read_wave_so(wave_order_id: str):
	"""Return the Sales Order's delivery_date + wave_delivery_type via one DB read, or None."""
	row = frappe.db.get_value(
		"Sales Order",
		{"wave_order_id": wave_order_id},
		["name", "delivery_date", "wave_delivery_type"],
		as_dict=True,
	)
	return row or None


def _maybe_log_heterogeneous_wave_sources(doc, wave_order_ids: list[str], primary) -> None:
	"""Warn when secondary Wave SOs disagree with the first on delivery_date / type."""
	if len(wave_order_ids) < 2:
		return
	divergences: list[str] = []
	for wave_order_id in wave_order_ids[1:]:
		other = _read_wave_so(wave_order_id)
		if not other:
			continue
		if other.get("delivery_date") != primary.get("delivery_date"):
			divergences.append(
				f"{wave_order_id}: delivery_date {other.get('delivery_date')} "
				f"vs primary {primary.get('delivery_date')}"
			)
		if (other.get("wave_delivery_type") or "") != (primary.get("wave_delivery_type") or ""):
			divergences.append(
				f"{wave_order_id}: wave_delivery_type {other.get('wave_delivery_type')!r} "
				f"vs primary {primary.get('wave_delivery_type')!r}"
			)
	if not divergences:
		return
	log_step(
		correlation_id=new_correlation_id(),
		step=STEP_AUTOPOPULATE_HETEROGENEOUS,
		level="Warning",
		doc_type=doc.doctype,
		linked_doctype=doc.doctype,
		linked_docname=doc.name or "<new>",
		wave_id=wave_order_ids[0],
		request_body={"wave_order_ids": wave_order_ids, "divergences": divergences},
		error_message=(
			"Delivery Note spans multiple Wave-sourced Sales Orders with conflicting "
			"delivery_date or wave_delivery_type. First SO's values used; operator review."
		),
	)


def _log_autopopulated(doc, primary, autopopulated: dict[str, str]) -> None:
	"""Info audit row capturing which DN fields were stamped + which SO they came from."""
	log_step(
		correlation_id=new_correlation_id(),
		step=STEP_AUTOPOPULATED,
		level="Info",
		doc_type=doc.doctype,
		linked_doctype=doc.doctype,
		linked_docname=doc.name or "<new>",
		wave_id=primary.get("name"),
		request_body={"autopopulated": autopopulated, "source_so": primary.get("name")},
	)


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
