"""Whitelisted Pick List actions — currently the manual batch-IDs push button.

`push_batch_ids_now` mirrors the after_insert auto-fire path but is invoked by
an explicit operator click. Bypasses `pick_list_batch_ids_push_enabled` (the
button itself is the consent) but still enforces the outbound HTTP config and
returns a structured payload the JS button can render an alert from.
"""

from __future__ import annotations

import frappe
from frappe import _

from wave_sync_hypa.wave_sync_hypa.handlers import pick_list as pl_handler
from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step

STEP_MANUAL_TRIGGER_REQUESTED = "pick_list_batch_ids_push_manual_trigger_requested"
STEP_MANUAL_TRIGGER_NO_WAVE_ORDERS = "pick_list_batch_ids_push_manual_trigger_no_wave_orders"


@frappe.whitelist()
def push_batch_ids_now(pick_list: str) -> dict:
	"""Manually enqueue the batch-IDs PATCH for one Pick List, regardless of the kill-switch.

	Returns:
	  {"ok": True, "enqueued": <int>, "correlation_id": "<uuid>"} on success.
	  {"ok": False, "reason": "<message>"} when nothing was queued.
	"""
	doc = frappe.get_doc("Pick List", pick_list)
	doc.check_permission("write")

	wave_ids = pl_handler._collect_distinct_wave_order_ids(doc)
	if not wave_ids and doc.get("wave_order_id"):
		wave_ids = [doc.wave_order_id]
	correlation_id = new_correlation_id()

	if not wave_ids:
		log_step(
			correlation_id=correlation_id,
			step=STEP_MANUAL_TRIGGER_NO_WAVE_ORDERS,
			level="Warning",
			doc_type="Pick List",
			linked_doctype="Pick List",
			linked_docname=doc.name,
			error_message="Manual batch-IDs trigger: Pick List has no Wave-sourced Sales Orders.",
		)
		return {"ok": False, "reason": _("Pick List has no Wave-sourced Sales Orders.")}

	settings = frappe.get_cached_doc("Wave Settings")
	grouped = pl_handler._group_batches_by_wave_order(doc, wave_ids, settings)

	enqueued = 0
	skipped_no_batches: list[str] = []
	for wave_order_id in wave_ids:
		products_data = grouped.get(wave_order_id) or []
		if not products_data:
			skipped_no_batches.append(wave_order_id)
			log_step(
				correlation_id=correlation_id,
				step=pl_handler.STEP_BATCH_IDS_NO_BATCHES_TO_PUSH,
				level="Info",
				doc_type="Pick List",
				linked_doctype="Pick List",
				linked_docname=doc.name,
				wave_id=wave_order_id,
				error_message="Manual trigger: no items with batch numbers found for this Wave order.",
			)
			continue
		frappe.enqueue(
			pl_handler.BATCH_PUSHER_DOTTED_PATH,
			queue="default",
			job_name=f"pick_list_batch_ids:{doc.name}:{wave_order_id}:manual:{correlation_id}",
			pick_list_name=doc.name,
			wave_order_id=wave_order_id,
			products_data=products_data,
			correlation_id=correlation_id,
			manual_trigger=True,
		)
		enqueued += 1
		log_step(
			correlation_id=correlation_id,
			step=STEP_MANUAL_TRIGGER_REQUESTED,
			level="Info",
			doc_type="Pick List",
			linked_doctype="Pick List",
			linked_docname=doc.name,
			wave_id=wave_order_id,
			request_body={"products_data": products_data, "manual_trigger": True},
		)

	if not enqueued:
		return {
			"ok": False,
			"reason": _(
				"No items with batch numbers were found on this Pick List "
				"(non-batch-tracked rows are excluded)."
			),
		}

	return {
		"ok": True,
		"enqueued": enqueued,
		"correlation_id": correlation_id,
		"skipped_no_batches": skipped_no_batches,
	}
