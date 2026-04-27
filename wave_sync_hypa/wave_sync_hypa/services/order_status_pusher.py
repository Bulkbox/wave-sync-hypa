"""Worker job: PUT a resolved status payload to Wave for one Sales Order.

Mirrors the stock_pusher pattern: re-reads Wave Settings on every invocation
(mid-queue kill-switch safety), validates outbound config, calls
wave_client.put_order_update, and logs every transition. Top-level try/except
wraps the body so an unexpected exception in one job never breaks the worker
loop.
"""

from __future__ import annotations

import frappe

from wave_sync_hypa.wave_sync_hypa.services import wave_client
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveOutboundError

STEP_PUSH_ATTEMPT = "order_status_push_attempt"
STEP_PUSH_SUCCESS = "order_status_push_success"
STEP_PUSH_FAILED = "order_status_push_failed"
STEP_PUSH_ABORTED_DISABLED = "order_status_push_aborted_settings_off"
STEP_PUSH_ABORTED_MISSING_CONFIG = "order_status_push_aborted_missing_config"
STEP_PUSH_ABORTED_NO_WAVE_ID = "order_status_push_aborted_no_wave_order_id"
STEP_PUSH_UNEXPECTED_ERROR = "order_status_push_unexpected_error"


def push_order_status(
	sales_order_name: str,
	event: str,
	payload: dict,
	correlation_id: str,
) -> None:
	"""Job entry point: PUT the resolved status payload to Wave for one SO; never raises."""
	try:
		_push_inner(sales_order_name, event, payload, correlation_id)
	except Exception as exc:
		log_step(
			correlation_id=correlation_id,
			step=STEP_PUSH_UNEXPECTED_ERROR,
			level="Error",
			doc_type="Sales Order",
			action=event,
			linked_doctype="Sales Order",
			linked_docname=sales_order_name,
			error_message=f"unexpected exception in push_order_status: {exc}",
			stack_trace=frappe.get_traceback(),
		)


def _push_inner(sales_order_name: str, event: str, payload: dict, correlation_id: str) -> None:
	"""Real work: validate config, resolve wave_order_id, call Wave, log transitions."""
	settings = frappe.get_cached_doc("Wave Settings")

	if not settings.get("outbound_order_status_sync_enabled"):
		log_step(
			correlation_id=correlation_id,
			step=STEP_PUSH_ABORTED_DISABLED,
			level="Warning",
			doc_type="Sales Order",
			action=event,
			linked_doctype="Sales Order",
			linked_docname=sales_order_name,
			error_message="outbound_order_status_sync_enabled is off; skipping push.",
		)
		return

	wave_order_id = frappe.db.get_value("Sales Order", sales_order_name, "wave_order_id")
	if not wave_order_id:
		log_step(
			correlation_id=correlation_id,
			step=STEP_PUSH_ABORTED_NO_WAVE_ID,
			level="Warning",
			doc_type="Sales Order",
			action=event,
			linked_doctype="Sales Order",
			linked_docname=sales_order_name,
			error_message="Sales Order has no wave_order_id at run time; cannot push.",
		)
		return

	config = _resolve_outbound_config(settings)
	if config is None:
		log_step(
			correlation_id=correlation_id,
			step=STEP_PUSH_ABORTED_MISSING_CONFIG,
			level="Error",
			doc_type="Sales Order",
			action=event,
			linked_doctype="Sales Order",
			linked_docname=sales_order_name,
			wave_id=wave_order_id,
			error_message="Wave outbound config incomplete (base_url / api_key / app_id).",
		)
		return

	skip_webhook = bool(settings.get("outbound_skip_webhook_notification"))
	log_step(
		correlation_id=correlation_id,
		step=STEP_PUSH_ATTEMPT,
		level="Info",
		doc_type="Sales Order",
		action=event,
		linked_doctype="Sales Order",
		linked_docname=sales_order_name,
		wave_id=wave_order_id,
		request_body={
			"order_id": wave_order_id,
			"body": payload,
			"skip_webhook_notification": skip_webhook,
		},
	)

	try:
		response = wave_client.put_order_update(
			base_url=config["base_url"],
			api_key=config["api_key"],
			app_id=config["app_id"],
			order_id=wave_order_id,
			body=payload,
			skip_webhook_notification=skip_webhook,
		)
	except WaveOutboundError as exc:
		log_step(
			correlation_id=correlation_id,
			step=STEP_PUSH_FAILED,
			level="Error",
			doc_type="Sales Order",
			action=event,
			linked_doctype="Sales Order",
			linked_docname=sales_order_name,
			wave_id=wave_order_id,
			request_body={"order_id": wave_order_id, "body": payload},
			error_message=str(exc),
			stack_trace=frappe.get_traceback(),
		)
		return

	log_step(
		correlation_id=correlation_id,
		step=STEP_PUSH_SUCCESS,
		level="Info",
		doc_type="Sales Order",
		action=event,
		linked_doctype="Sales Order",
		linked_docname=sales_order_name,
		wave_id=wave_order_id,
		request_body={"order_id": wave_order_id, "body": payload},
		response_body=response,
	)


def _resolve_outbound_config(settings) -> dict | None:
	"""Pull the three values the HTTP call needs; return None if any required piece is missing."""
	base_url = (settings.get("wave_api_base_url") or "").strip()
	app_id = (settings.get("wave_app_id") or "").strip()
	api_key = settings.get_password("wave_api_key", raise_exception=False) or ""
	if not (base_url and app_id and api_key):
		return None
	return {"base_url": base_url, "app_id": app_id, "api_key": api_key}
