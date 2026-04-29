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
	"""Single-source dispatch: read doc.wave_order_id, hand off to the fan-out helper.

	The Sales Order on_submit / on_cancel paths walk through here. The DN /
	SI handlers (which can carry items from multiple Wave-sourced SOs in a
	single document) call dispatch_with_wave_order_ids directly with their
	own pre-computed list.
	"""
	wave_order_id = doc.get("wave_order_id") or ""
	dispatch_with_wave_order_ids(doc, event, [wave_order_id] if wave_order_id else [])


def dispatch_with_wave_order_ids(
	doc,
	event: str,
	wave_order_ids: list[str],
	*,
	forced_payload: dict | None = None,
) -> None:
	"""Resolve rules once for this (doctype, event) and enqueue one push per wave_order_id.

	Single correlation_id covers the whole emit so all log rows for the
	dispatch — setup, skips, every per-leg enqueue — chain together. Per-row
	traceability for which Wave order each leg targeted is preserved on the
	`wave_id` column of each log row.

	Multi-leg fan-out is the DN / SI use case: one ERP document produced from
	items belonging to several Wave-sourced Sales Orders. Each leg becomes
	an independent worker job so a transient failure on one Wave order
	doesn't block the others.

	`forced_payload` lets callers bypass the rule resolver when the desired
	transition cannot be expressed as a (doctype, event, condition) row —
	for example, "Sales Invoice with is_return=1 AND grand_total matches the
	original invoice within 1 cent" requires comparing two documents, which
	the rule schema can't model. The caller computes the payload itself and
	passes it in. The skip semantics for disabled / no-wave-id remain in
	effect; only the resolver step is bypassed.
	"""
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

	if not wave_order_ids:
		log_step(
			correlation_id=correlation_id,
			step=STEP_SKIPPED_NO_WAVE_ID,
			level="Info",
			doc_type=doc.doctype,
			action=event,
			linked_doctype=doc.doctype,
			linked_docname=doc.name,
			error_message=f"{doc.doctype} has no Wave-sourced order to push status to.",
		)
		return

	if forced_payload is None:
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
				wave_id=wave_order_ids[0] if len(wave_order_ids) == 1 else None,
				error_message=f"No enabled outbound status rule matched ({doc.doctype}, {event}).",
			)
			return
	else:
		payload = forced_payload

	for wave_order_id in wave_order_ids:
		_enqueue_push(doc.doctype, doc.name, event, payload, correlation_id, wave_order_id)


def _enqueue_push(
	source_doctype: str,
	source_docname: str,
	event: str,
	payload: dict,
	correlation_id: str,
	wave_order_id: str,
) -> None:
	"""Queue the worker job, baking the resolved payload + source-doc identity into the kwargs.

	`source_doctype` and `source_docname` identify the ERP document that
	triggered this push (Sales Order / Delivery Note / Sales Invoice). They
	are plumbed into both the log rows and the worker so the Dynamic Link
	on Wave Sync Log (`linked_doctype` + `linked_docname`) resolves to the
	*actual* triggering document — clicking through the audit trail jumps
	to the DN that fired the dispatch, not a phantom "Sales Order" row.

	Note: the worker function expects `erp_event`, not `event`. `event` is a
	reserved kwarg in frappe.enqueue's own signature (used for scheduled-job
	semantics) and Frappe consumes it before forwarding to the worker. Pass
	the value through as `erp_event` so it survives the call.
	"""
	try:
		frappe.enqueue(
			WORKER_DOTTED_PATH,
			queue="default",
			enqueue_after_commit=True,
			source_doctype=source_doctype,
			source_docname=source_docname,
			erp_event=event,
			payload=payload,
			correlation_id=correlation_id,
			wave_order_id=wave_order_id,
		)
	except Exception as exc:
		log_step(
			correlation_id=correlation_id,
			step=STEP_ENQUEUE_FAILED,
			level="Error",
			doc_type=source_doctype,
			action=event,
			linked_doctype=source_doctype,
			linked_docname=source_docname,
			wave_id=wave_order_id,
			error_message=f"failed to enqueue order-status push: {exc}",
			stack_trace=frappe.get_traceback(),
		)
		return

	log_step(
		correlation_id=correlation_id,
		step=STEP_ENQUEUED,
		level="Info",
		doc_type=source_doctype,
		action=event,
		linked_doctype=source_doctype,
		linked_docname=source_docname,
		wave_id=wave_order_id,
		request_body={"event": event, "payload": payload},
	)
