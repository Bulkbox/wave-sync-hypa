"""ERP-side hooks: enqueue an outbound Wave PUT when a Sales Order's lifecycle changes.

Two entry points wired in hooks.py:
  - on_sales_order_submit  (Sales Order.on_submit)
  - on_sales_order_cancel  (Sales Order.on_cancel)

Each one consults the operator-editable rule table on Wave Settings and, if
a rule matches, enqueues a worker job carrying the resolved {status?,
deliveryStatus?} payload. The matching decision is captured in the
order_status_push_enqueued log row so the audit trail is consistent end to
end.
"""

from __future__ import annotations

import frappe

from wave_sync_hypa.wave_sync_hypa.services import order_status_resolver
from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step

STEP_ENQUEUED = "order_status_push_enqueued"
STEP_SKIPPED_DISABLED = "order_status_push_skipped_disabled"
STEP_SKIPPED_NO_WAVE_ID = "order_status_push_skipped_no_wave_order_id"
STEP_SKIPPED_NO_RULE = "order_status_push_skipped_no_matching_rule"
STEP_ENQUEUE_FAILED = "order_status_push_enqueue_failed"

WORKER_DOTTED_PATH = "wave_sync_hypa.wave_sync_hypa.services.order_status_pusher.push_order_status"


def on_sales_order_submit(doc, method=None) -> None:
	"""Sales Order.on_submit doc_event: enqueue a Wave status PUT if rules match."""
	_dispatch(doc, "submit")


def on_sales_order_cancel(doc, method=None) -> None:
	"""Sales Order.on_cancel doc_event: enqueue a Wave status PUT if rules match."""
	_dispatch(doc, "cancel")


def _dispatch(doc, event: str) -> None:
	"""Resolve rules and enqueue, or log the precise reason for skipping."""
	correlation_id = new_correlation_id()
	settings = frappe.get_cached_doc("Wave Settings")

	if not settings.get("outbound_order_status_sync_enabled"):
		log_step(
			correlation_id=correlation_id,
			step=STEP_SKIPPED_DISABLED,
			level="Info",
			doc_type=doc.doctype,
			action=event,
			linked_doctype=doc.doctype,
			linked_docname=doc.name,
			error_message="outbound_order_status_sync_enabled is off.",
		)
		return

	wave_order_id = doc.get("wave_order_id") or ""
	if not wave_order_id:
		log_step(
			correlation_id=correlation_id,
			step=STEP_SKIPPED_NO_WAVE_ID,
			level="Info",
			doc_type=doc.doctype,
			action=event,
			linked_doctype=doc.doctype,
			linked_docname=doc.name,
			error_message="Sales Order has no wave_order_id; not a Wave-sourced order.",
		)
		return

	payload = order_status_resolver.resolve_outbound_payload(doc, event, settings)
	if not payload:
		log_step(
			correlation_id=correlation_id,
			step=STEP_SKIPPED_NO_RULE,
			level="Info",
			doc_type=doc.doctype,
			action=event,
			linked_doctype=doc.doctype,
			linked_docname=doc.name,
			wave_id=wave_order_id,
			error_message=f"No enabled outbound status rule matched ({doc.doctype}, {event}).",
		)
		return

	_enqueue_push(doc.name, event, payload, correlation_id, wave_order_id)


def _enqueue_push(sales_order_name: str, event: str, payload: dict, correlation_id: str, wave_order_id: str) -> None:
	"""Queue the worker job, baking the resolved payload into the kwargs."""
	try:
		frappe.enqueue(
			WORKER_DOTTED_PATH,
			queue="default",
			enqueue_after_commit=True,
			sales_order_name=sales_order_name,
			event=event,
			payload=payload,
			correlation_id=correlation_id,
		)
	except Exception as exc:
		log_step(
			correlation_id=correlation_id,
			step=STEP_ENQUEUE_FAILED,
			level="Error",
			doc_type="Sales Order",
			action=event,
			linked_doctype="Sales Order",
			linked_docname=sales_order_name,
			wave_id=wave_order_id,
			error_message=f"failed to enqueue order-status push: {exc}",
			stack_trace=frappe.get_traceback(),
		)
		return

	log_step(
		correlation_id=correlation_id,
		step=STEP_ENQUEUED,
		level="Info",
		doc_type="Sales Order",
		action=event,
		linked_doctype="Sales Order",
		linked_docname=sales_order_name,
		wave_id=wave_order_id,
		request_body={"event": event, "payload": payload},
	)
