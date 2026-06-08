"""Pick List hook: route the configured outbound channels on Pick List creation.

Wired in hooks.py:

  Pick List.validate     -> stamp_wave_order_id    (filterability only)
  Pick List.after_insert -> after_pick_list_insert (the real work)

Pick List submit / cancel are intentionally NOT wired. Wave's interest in a
Pick List is "the order has been accepted and these items, with these batch
ids, are about to be dispatched". That information is fully known at the
moment of creation: by the time after_insert fires, the locations rows have
been persisted (children commit in the same transaction as the parent), so
we can read them and emit the right calls. Submitting a Pick List adds no
new information for Wave.

Two independent channels fire from this handler:

  - Status push: routed through the standard outbound rules table. The
    expected operator config is one (Pick List, after_insert, ACCEPTED) row
    in Wave Settings -> Outbound Status Rules.

  - Batch-IDs push: gated by the pick_list_batch_ids_push_enabled Check on
    Wave Settings. When on, we group locations[] rows by linked Sales
    Order's wave_order_id, then by item_code, collect distinct batch_no
    values per item, and enqueue one batch_pusher worker per Wave order.
    All Wave HTTP calls happen in the worker so form-save latency is
    unchanged.

The two channels are orthogonal. Status off / batch-IDs on, both off, both
on, status on / batch-IDs off — all valid configurations.
"""

from __future__ import annotations

import frappe

from wave_sync_hypa.wave_sync_hypa.handlers import order_status
from wave_sync_hypa.wave_sync_hypa.services import picker_identifier
from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step
from wave_sync_hypa.wave_sync_hypa.services.master_switch import skip_if_disabled
from wave_sync_hypa.wave_sync_hypa.services.wave_order_ids import (
	child_row_field,
	dedupe_preserving_order,
	wave_order_id_of,
)

STEP_STAMP_MULTI_SOURCE = "pick_list_wave_order_id_multi_source"
STEP_NO_WAVE_ORDERS = "pick_list_no_wave_sourced_orders"
STEP_BATCH_IDS_ENQUEUED = "pick_list_batch_ids_push_enqueued"
STEP_BATCH_IDS_ENQUEUE_FAILED = "pick_list_batch_ids_push_enqueue_failed"
STEP_BATCH_IDS_NO_BATCHES_TO_PUSH = "pick_list_batch_ids_push_no_batches_to_push"
STEP_BATCH_IDS_IDENTIFIER_FAILED = "pick_list_batch_ids_push_identifier_lookup_failed"
STEP_SUBMIT_BLOCKED = "pick_list_submit_blocked"
STEP_CANCEL_BLOCKED = "pick_list_cancel_blocked"

BATCH_PUSHER_DOTTED_PATH = (
	"wave_sync_hypa.wave_sync_hypa.services.pick_list_batch_pusher.push_pick_list_batch_ids"
)

# Role that lets a human submit/cancel a Pick List from ERP when the
# pick_list_erp_submit_lockdown_enabled kill-switch is on. System Manager
# always passes the gate too, but that role is too privileged to be the
# everyday answer for warehouse leads — this dedicated role is the one ops
# actually grants.
PICK_LIST_OVERRIDE_ROLE = "Pick List Wave Override"

# Forward-compat seam: the future inbound webhook handler will set this flag
# right before calling doc.submit() so the gate lets it through. Nothing in
# this module sets the flag yet — it's intentionally read-only here so the
# webhook PR is a one-line change.
INBOUND_SUBMIT_FLAG = "wave_inbound_pick_list_submit"


def stamp_wave_order_id(doc, method=None) -> None:
	"""Validate hook: copy the first reachable wave_order_id onto the Pick List.

	Idempotent — bails out when the field is already populated. Pure stamping
	for desk filterability; emits no outbound traffic and is safe to leave on
	regardless of any kill-switch.
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
				f"Pick List spans {len(wave_ids)} distinct Wave-sourced Sales Orders. "
				"Stamping the first on wave_order_id; outbound pushes still fan out to all."
			),
		)


def after_pick_list_insert(doc, method=None) -> None:
	"""after_insert hook: route status via the rules table; batch-IDs gated by its own kill-switch."""
	if skip_if_disabled(
		new_correlation_id(),
		doc_type=doc.doctype,
		linked_doctype=doc.doctype,
		linked_docname=doc.name,
	):
		return
	wave_ids = _collect_distinct_wave_order_ids(doc)
	if not wave_ids and doc.get("wave_order_id"):
		wave_ids = [doc.wave_order_id]
	if not wave_ids:
		log_step(
			correlation_id=new_correlation_id(),
			step=STEP_NO_WAVE_ORDERS,
			level="Info",
			doc_type=doc.doctype,
			linked_doctype=doc.doctype,
			linked_docname=doc.name,
			error_message="Pick List has no Wave-sourced Sales Orders to push to.",
		)
		return

	order_status.dispatch_with_wave_order_ids(doc, "after_insert", wave_ids)

	settings = frappe.get_cached_doc("Wave Settings")
	if settings.get("pick_list_batch_ids_push_enabled"):
		_enqueue_batch_ids_pushes(doc, wave_ids, settings)


def _enqueue_batch_ids_pushes(doc, wave_ids: list[str], settings) -> None:
	"""For each Wave order, build per-item identifier lists and enqueue one worker job."""
	correlation_id = new_correlation_id()
	grouped = _group_batches_by_wave_order(doc, wave_ids, settings)
	for wave_order_id in wave_ids:
		products_data = grouped.get(wave_order_id) or []
		if not products_data:
			log_step(
				correlation_id=correlation_id,
				step=STEP_BATCH_IDS_NO_BATCHES_TO_PUSH,
				level="Info",
				doc_type=doc.doctype,
				linked_doctype=doc.doctype,
				linked_docname=doc.name,
				wave_id=wave_order_id,
				error_message="No items with batch numbers found for this Wave order.",
			)
			continue
		try:
			frappe.enqueue(
				BATCH_PUSHER_DOTTED_PATH,
				queue="default",
				enqueue_after_commit=True,
				job_name=f"pick_list_batch_ids:{doc.name}:{wave_order_id}",
				pick_list_name=doc.name,
				wave_order_id=wave_order_id,
				products_data=products_data,
				correlation_id=correlation_id,
			)
		except Exception as exc:
			log_step(
				correlation_id=correlation_id,
				step=STEP_BATCH_IDS_ENQUEUE_FAILED,
				level="Error",
				doc_type=doc.doctype,
				linked_doctype=doc.doctype,
				linked_docname=doc.name,
				wave_id=wave_order_id,
				error_message=f"failed to enqueue Pick List batch-IDs push: {exc}",
				stack_trace=frappe.get_traceback(),
			)
			continue
		log_step(
			correlation_id=correlation_id,
			step=STEP_BATCH_IDS_ENQUEUED,
			level="Info",
			doc_type=doc.doctype,
			linked_doctype=doc.doctype,
			linked_docname=doc.name,
			wave_id=wave_order_id,
			request_body={"products_data": products_data},
		)


def _group_batches_by_wave_order(doc, wave_ids: list[str], settings) -> dict[str, list[dict]]:
	"""Walk locations[] once, return {wave_order_id: [{item_code, batch_ids}, ...]} preserving row order.

	The `batch_ids` field is the value Wave's picker app scans for each Pick
	List line; its meaning is governed by `Wave Settings.picker_identifier_source`:

	  * blank          -> one entry per row's batch_no (today's behaviour;
	                       preserves ERPNext FEFO/FIFO allocation).
	  * "Item Code"    -> [item_code] (single element, consolidates the SKU's rows).
	  * "Item Barcode" -> [Item.barcodes[0].barcode] (single element); the
	                       picker_identifier helper raises when the Item has
	                       no barcode row, surfacing the misconfiguration.

	Items whose identifier list is empty under the active mode are excluded
	from the payload — for batch mode that means non-batch-tracked rows
	contribute nothing, which preserves existing behaviour.
	"""
	so_to_wave = _build_so_to_wave_map(doc)
	allowed = set(wave_ids)
	rows_by_order_and_item: dict[str, dict[str, list]] = {}
	for row in doc.get("locations") or []:
		so_name = _row_field(row, "sales_order")
		if not so_name:
			continue
		wave_order_id = (so_to_wave.get(so_name) or "").strip()
		if not wave_order_id or wave_order_id not in allowed:
			continue
		item_code = _row_field(row, "item_code")
		if not item_code:
			continue
		rows_by_order_and_item.setdefault(wave_order_id, {}).setdefault(item_code, []).append(row)

	grouped: dict[str, list[dict]] = {}
	correlation_id = new_correlation_id()
	for wave_order_id, items in rows_by_order_and_item.items():
		entries: list[dict] = []
		for item_code, rows in items.items():
			try:
				identifiers = picker_identifier.identifiers_for_sku_outbound(rows, settings)
			except frappe.ValidationError as exc:
				log_step(
					correlation_id=correlation_id,
					step=STEP_BATCH_IDS_IDENTIFIER_FAILED,
					level="Error",
					doc_type=doc.doctype,
					linked_doctype=doc.doctype,
					linked_docname=doc.name,
					wave_id=wave_order_id,
					request_body={"item_code": item_code},
					error_message=str(exc),
				)
				continue
			if not identifiers:
				continue
			entries.append(
				{
					"item_code": item_code,
					"batch_ids": identifiers,
					"comments": picker_identifier.comment_for_sku_outbound(rows),
				}
			)
		if entries:
			grouped[wave_order_id] = entries
	return grouped


def _build_so_to_wave_map(doc) -> dict[str, str]:
	"""One DB roundtrip per distinct Sales Order on the Pick List; map name -> wave_order_id."""
	so_names = {_row_field(row, "sales_order") for row in doc.get("locations") or []}
	so_names.discard("")
	if not so_names:
		return {}
	return {
		so_name: (frappe.db.get_value("Sales Order", so_name, "wave_order_id") or "") for so_name in so_names
	}


def _collect_distinct_wave_order_ids(doc) -> list[str]:
	"""Return unique Wave order ids reachable from this Pick List's rows, in row order."""
	return dedupe_preserving_order(
		wave_order_id_of("Sales Order", _row_field(row, "sales_order"))
		for row in doc.get("locations") or []
	)


# This handler and pe_references share one child-row accessor.
_row_field = child_row_field


def block_unprivileged_pick_list_submit(doc, method=None) -> None:
	"""before_submit guard: refuse ERP-side submits unless the user is privileged.

	Pick Lists are Wave's territory once the lockdown is on: picking happens
	in the Wave app, and a (future) inbound webhook will call doc.submit()
	with the INBOUND_SUBMIT_FLAG set. Direct submits from the Desk become a
	manager-only escape hatch. When the lockdown setting is off, this is a
	no-op so existing sites keep working until ops flips the switch.
	"""
	_enforce_pick_list_action_gate(doc, action="submit", step=STEP_SUBMIT_BLOCKED)


def block_unprivileged_pick_list_cancel(doc, method=None) -> None:
	"""before_cancel guard: mirror of submit gate, same reasoning."""
	_enforce_pick_list_action_gate(doc, action="cancel", step=STEP_CANCEL_BLOCKED)


def _enforce_pick_list_action_gate(doc, *, action: str, step: str) -> None:
	"""Common gate: pass when lockdown off / inbound flag set / non-Wave PL / user privileged.

	The lockdown's purpose is to make Wave's picker app the source of truth for
	Wave-sourced Pick Lists. PLs with no wave_order_id stamp (offline orders —
	walk-in, phone-in, manual ERP) have no Wave-side picking workflow to defer
	to, so the gate doesn't apply to them. stamp_wave_order_id runs on validate
	and populates wave_order_id for any PL with at least one Wave-linked SO row,
	so by the time before_submit fires the stamp is fresh and authoritative.
	"""
	settings = frappe.get_cached_doc("Wave Settings")
	if not settings.get("pick_list_erp_submit_lockdown_enabled"):
		return
	if frappe.flags.get(INBOUND_SUBMIT_FLAG):
		return
	if not (doc.get("wave_order_id") or "").strip():
		return  # Non-Wave Pick List — lockdown does not apply.
	user = frappe.session.user
	roles = set(frappe.get_roles(user))
	if PICK_LIST_OVERRIDE_ROLE in roles or "System Manager" in roles:
		return

	log_step(
		correlation_id=new_correlation_id(),
		step=step,
		level="Warning",
		doc_type=doc.doctype,
		linked_doctype=doc.doctype,
		linked_docname=doc.name,
		wave_id=doc.get("wave_order_id") or None,
		error_message=(
			f"User '{user}' attempted to {action} this Pick List from ERP while the Wave "
			f"submit lockdown is on. Action blocked. Assign the '{PICK_LIST_OVERRIDE_ROLE}' "
			"role to allow manual overrides."
		),
	)
	frappe.throw(
		msg=(
			"Pick Lists are submitted by Wave once picking is complete. "
			f"Ask a Wave Operations Manager if you need to {action} this Pick List manually."
		),
		title="Pick List action restricted",
		exc=frappe.PermissionError,
	)
