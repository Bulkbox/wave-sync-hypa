"""Worker job: POST a resolved status transition to Wave for one Sales Order.

Wave's status endpoint is path-keyed:

    POST /api/v3/admin/orders/{order_id}/status/{status_name}

There is no body, no query string, and no equivalent endpoint for
deliveryStatus yet. The resolver may emit a payload carrying
{"status": "...", "deliveryStatus": "..."} based on rule rows; this
worker pushes the status component via the supported endpoint and logs
+ skips the deliveryStatus component until Wave provides its endpoint.

Mirrors the stock_pusher pattern: re-reads Wave Settings on every
invocation (mid-queue kill-switch safety), validates outbound config,
calls wave_client.post_order_status, and logs every transition. Top-level
try/except wraps the body so an unexpected exception in one job never
breaks the worker loop.
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
STEP_PUSH_ABORTED_EMPTY_PAYLOAD = "order_status_push_aborted_empty_payload"
STEP_PUSH_DELIVERY_STATUS_UNSUPPORTED = "order_status_push_delivery_status_unsupported"
STEP_PUSH_UNEXPECTED_ERROR = "order_status_push_unexpected_error"
STEP_WORKER_STARTED = "order_status_push_worker_started"


def push_order_status(
	sales_order_name: str,
	erp_event: str,
	payload: dict,
	correlation_id: str,
) -> None:
	"""Job entry point: POST the resolved status transition to Wave for one SO; never raises.

	The kwarg is `erp_event` not `event` because `event` is reserved by
	frappe.enqueue's own signature (used for scheduled-job firing semantics).
	When we pass `event=...` to frappe.enqueue, Frappe consumes it before
	forwarding to this function, and the worker call dies with a missing-
	positional-argument TypeError before even entering the function body —
	which is below the try/except, so no _attempt log row is written, and
	the failure surfaces only in the Error Log.

	The first thing the function does is write _worker_started so any future
	"function never enters" bug is visible in the audit trail without having
	to grep Error Log.
	"""
	log_step(
		correlation_id=correlation_id,
		step=STEP_WORKER_STARTED,
		level="Info",
		doc_type="Sales Order",
		action=erp_event,
		linked_doctype="Sales Order",
		linked_docname=sales_order_name,
	)
	try:
		_push_inner(sales_order_name, erp_event, payload, correlation_id)
	except Exception as exc:
		log_step(
			correlation_id=correlation_id,
			step=STEP_PUSH_UNEXPECTED_ERROR,
			level="Error",
			doc_type="Sales Order",
			action=erp_event,
			linked_doctype="Sales Order",
			linked_docname=sales_order_name,
			error_message=f"unexpected exception in push_order_status: {exc}",
			stack_trace=frappe.get_traceback(),
		)


def _push_inner(sales_order_name: str, erp_event: str, payload: dict, correlation_id: str) -> None:
	"""Real work: validate config, resolve wave_order_id, POST status, log transitions."""
	settings = frappe.get_cached_doc("Wave Settings")

	if not settings.get("outbound_order_status_sync_enabled"):
		log_step(
			correlation_id=correlation_id,
			step=STEP_PUSH_ABORTED_DISABLED,
			level="Warning",
			doc_type="Sales Order",
			action=erp_event,
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
			action=erp_event,
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
			action=erp_event,
			linked_doctype="Sales Order",
			linked_docname=sales_order_name,
			wave_id=wave_order_id,
			error_message="Wave outbound config incomplete (base_url / api_key / app_id).",
		)
		return

	_warn_if_delivery_status_present(payload, sales_order_name, erp_event, correlation_id, wave_order_id)

	status_name = (payload or {}).get("status")
	if not status_name:
		# After deliveryStatus-only rules are warned about, there's nothing supported left to push.
		log_step(
			correlation_id=correlation_id,
			step=STEP_PUSH_ABORTED_EMPTY_PAYLOAD,
			level="Info",
			doc_type="Sales Order",
			action=erp_event,
			linked_doctype="Sales Order",
			linked_docname=sales_order_name,
			wave_id=wave_order_id,
			error_message="No supported field in resolved payload (status missing).",
			request_body={"resolved_payload": payload},
		)
		return

	_post_status(sales_order_name, erp_event, correlation_id, wave_order_id, config, status_name)


def _post_status(
	sales_order_name: str,
	erp_event: str,
	correlation_id: str,
	wave_order_id: str,
	config: dict,
	status_name: str,
) -> None:
	"""Build the path-keyed POST and log attempt + outcome."""
	url_path = f"/api/v3/admin/orders/{wave_order_id}/status/{status_name}"
	log_step(
		correlation_id=correlation_id,
		step=STEP_PUSH_ATTEMPT,
		level="Info",
		doc_type="Sales Order",
		action=erp_event,
		linked_doctype="Sales Order",
		linked_docname=sales_order_name,
		wave_id=wave_order_id,
		request_body={
			"method": "POST",
			"path": url_path,
			"order_id": wave_order_id,
			"status_name": status_name,
		},
	)

	try:
		response = wave_client.post_order_status(
			base_url=config["base_url"],
			api_key=config["api_key"],
			app_id=config["app_id"],
			order_id=wave_order_id,
			status_name=status_name,
		)
	except WaveOutboundError as exc:
		log_step(
			correlation_id=correlation_id,
			step=STEP_PUSH_FAILED,
			level="Error",
			doc_type="Sales Order",
			action=erp_event,
			linked_doctype="Sales Order",
			linked_docname=sales_order_name,
			wave_id=wave_order_id,
			request_body={"path": url_path, "status_name": status_name},
			error_message=str(exc),
			stack_trace=frappe.get_traceback(),
		)
		return

	log_step(
		correlation_id=correlation_id,
		step=STEP_PUSH_SUCCESS,
		level="Info",
		doc_type="Sales Order",
		action=erp_event,
		linked_doctype="Sales Order",
		linked_docname=sales_order_name,
		wave_id=wave_order_id,
		request_body={"path": url_path, "status_name": status_name},
		response_body=response,
	)


def _warn_if_delivery_status_present(
	payload: dict,
	sales_order_name: str,
	erp_event: str,
	correlation_id: str,
	wave_order_id: str,
) -> None:
	"""Log a clear unsupported-channel warning when a rule sets wave_delivery_status.

	Wave gave us only the path-keyed status endpoint. Until they provide the
	equivalent for deliveryStatus, we cannot push delivery transitions, and
	guessing a URL would 404 + clutter logs. This warning surfaces the gap
	in the audit trail so ops know exactly what was configured but skipped.
	"""
	delivery_status = (payload or {}).get("deliveryStatus")
	if not delivery_status:
		return
	log_step(
		correlation_id=correlation_id,
		step=STEP_PUSH_DELIVERY_STATUS_UNSUPPORTED,
		level="Warning",
		doc_type="Sales Order",
		action=erp_event,
		linked_doctype="Sales Order",
		linked_docname=sales_order_name,
		wave_id=wave_order_id,
		request_body={"requested_delivery_status": delivery_status},
		error_message=(
			"Wave does not yet provide an outbound endpoint for deliveryStatus. "
			"Skipping that field; status transitions still pushed normally."
		),
	)


def _resolve_outbound_config(settings) -> dict | None:
	"""Pull the three values the HTTP call needs; return None if any required piece is missing."""
	base_url = (settings.get("wave_api_base_url") or "").strip()
	app_id = (settings.get("wave_app_id") or "").strip()
	api_key = settings.get_password("wave_api_key", raise_exception=False) or ""
	if not (base_url and app_id and api_key):
		return None
	return {"base_url": base_url, "app_id": app_id, "api_key": api_key}
