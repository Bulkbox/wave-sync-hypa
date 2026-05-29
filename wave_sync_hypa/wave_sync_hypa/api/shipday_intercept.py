"""Override of order_stage_tracker's shipday_update_order_stage.order_stage.

n8n hits this URL when Shipday confirms a delivery state change:

  POST /api/method/order_stage_tracker.utils.shipday_update_order_stage.order_stage

`hooks.override_whitelisted_methods` re-routes the dotted path to this module
so n8n's call lands in our wrapper instead. The wrapper:

  1. Calls the upstream function FIRST. Upstream side effects (writing
     `custom_order_stage` on the Sales Order, syncing to CS-Cart) always
     land, regardless of which stage was computed.
  2. If upstream's result reports `new_stage == "Delivered"` AND the Sales
     Order has a `wave_order_id`, dispatches a Wave order-status push with
     forced_payload {"status": "COMPLETED"}.
  3. Wave dispatch is wrapped in try/except so a Wave outage never disturbs
     the upstream return value or breaks the CS-Cart sync.

"Failed" / "Partial Delivery" stages: ERP is updated normally, no Wave push.
"""

from __future__ import annotations

import frappe

from order_stage_tracker.utils.shipday_update_order_stage import (
	order_stage as _upstream_order_stage,
)

from wave_sync_hypa.wave_sync_hypa.handlers import order_status
from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step

STAGE_DELIVERED = "Delivered"
WAVE_STATUS_COMPLETED = "COMPLETED"
EVENT_SHIPDAY_DELIVERED = "shipday_delivered"

STEP_DISPATCHED = "shipday_delivered_dispatched"
STEP_FAILED = "shipday_delivered_dispatch_failed"


@frappe.whitelist()
def order_stage(delivery_note: str) -> dict:
	"""Run upstream order_stage, then push Wave COMPLETED on Delivered.

	Upstream runs first; its side effects always land. Wave dispatch runs
	only when the resulting stage is "Delivered" AND the SO is Wave-linked.
	Wave-side exceptions are swallowed + audited; the upstream return value
	is handed back to the caller unchanged.
	"""
	result = _upstream_order_stage(delivery_note)
	if (result or {}).get("new_stage") != STAGE_DELIVERED:
		return result
	sales_order = ((result or {}).get("sales_order") or "").strip()
	if not sales_order:
		return result
	wave_order_id = (
		frappe.db.get_value("Sales Order", sales_order, "wave_order_id") or ""
	).strip()
	if not wave_order_id:
		return result

	correlation_id = new_correlation_id()
	try:
		so = frappe.get_doc("Sales Order", sales_order)
		order_status.dispatch_with_wave_order_ids(
			so,
			EVENT_SHIPDAY_DELIVERED,
			[wave_order_id],
			forced_payload={"status": WAVE_STATUS_COMPLETED},
		)
		log_step(
			correlation_id=correlation_id,
			step=STEP_DISPATCHED,
			level="Success",
			doc_type="Sales Order",
			linked_doctype="Sales Order",
			linked_docname=sales_order,
			wave_id=wave_order_id,
		)
	except Exception as exc:
		log_step(
			correlation_id=correlation_id,
			step=STEP_FAILED,
			level="Error",
			doc_type="Sales Order",
			linked_doctype="Sales Order",
			linked_docname=sales_order,
			wave_id=wave_order_id,
			error_message=f"Wave dispatch from shipday override failed: {exc}",
			stack_trace=frappe.get_traceback(),
		)
	return result
