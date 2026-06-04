"""Worker: PATCH a Wave order's `paymentStatus` field.

Triggered by `handlers.payment_entry.on_payment_entry_submit` when the
resolver decides a Wave order is now fully settled. The handler fans out
one job per Wave order touched by the PE; this worker's job is to:

  1. Re-read Wave Settings (mid-queue kill-switch safety).
  2. Validate the outbound config triplet (base_url / api_key / app_id).
  3. PATCH `/api/v3/admin/orders/{wave_order_id}` with the supplied body —
     a `{"paymentStatus": <enum>}` dict per the caller.
  4. Log every transition via the Wave Sync Log audit pattern.

Top-level try/except wraps the body so an unexpected exception in one job
never breaks the worker loop. Same defensive shape as the other outbound
workers (`order_status_pusher`, `pick_list_batch_pusher`).
"""

from __future__ import annotations

import frappe

from wave_sync_hypa.wave_sync_hypa.services import wave_client
from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step
from wave_sync_hypa.wave_sync_hypa.services.master_switch import (
	STEP_MASTER_DISABLED,
	is_wave_integration_enabled,
)
from wave_sync_hypa.wave_sync_hypa.services.wave_config import resolve_outbound_config
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveOutboundError

WORKER_DOTTED_PATH = (
	"wave_sync_hypa.wave_sync_hypa.services.payment_status_pusher.push_payment_status"
)

STEP_ENQUEUED = "payment_status_push_enqueued"
STEP_ENQUEUE_FAILED = "payment_status_push_enqueue_failed"
STEP_WORKER_STARTED = "payment_status_push_worker_started"
STEP_ATTEMPT = "payment_status_push_attempt"
STEP_SUCCESS = "payment_status_push_success"
STEP_RESPONSE_MISMATCH = "payment_status_push_response_mismatch"
STEP_FAILED = "payment_status_push_failed"
STEP_ABORTED_MISSING_CONFIG = "payment_status_push_aborted_missing_config"
STEP_UNEXPECTED_ERROR = "payment_status_push_unexpected_error"


def enqueue_payment_status_push(
	pe_doc,
	wave_order_id: str,
	payment_status: str,
	correlation_id: str | None = None,
) -> None:
	"""Entry point for the PE handler: enqueue one async PATCH for this Wave order."""
	correlation_id = correlation_id or new_correlation_id()
	try:
		frappe.enqueue(
			WORKER_DOTTED_PATH,
			queue="default",
			enqueue_after_commit=True,
			job_name=f"payment_status_push:{pe_doc.name}:{wave_order_id}",
			pe_name=pe_doc.name,
			wave_order_id=wave_order_id,
			payment_status=payment_status,
			correlation_id=correlation_id,
		)
	except Exception as exc:
		log_step(
			correlation_id=correlation_id,
			step=STEP_ENQUEUE_FAILED,
			level="Error",
			doc_type="Payment Entry",
			linked_doctype="Payment Entry",
			linked_docname=pe_doc.name,
			wave_id=wave_order_id,
			error_message=f"failed to enqueue payment status push: {exc}",
			stack_trace=frappe.get_traceback(),
		)
		return
	log_step(
		correlation_id=correlation_id,
		step=STEP_ENQUEUED,
		level="Info",
		doc_type="Payment Entry",
		linked_doctype="Payment Entry",
		linked_docname=pe_doc.name,
		wave_id=wave_order_id,
		request_body={"paymentStatus": payment_status},
	)


def push_payment_status(
	*,
	pe_name: str,
	wave_order_id: str,
	payment_status: str,
	correlation_id: str,
) -> None:
	"""Worker entry point: PATCH the Wave order's paymentStatus. Never raises."""
	log_step(
		correlation_id=correlation_id,
		step=STEP_WORKER_STARTED,
		level="Info",
		doc_type="Payment Entry",
		linked_doctype="Payment Entry",
		linked_docname=pe_name,
		wave_id=wave_order_id or None,
	)
	try:
		if not is_wave_integration_enabled():
			log_step(
				correlation_id=correlation_id,
				step=STEP_MASTER_DISABLED,
				level="Info",
				doc_type="Payment Entry",
				linked_doctype="Payment Entry",
				linked_docname=pe_name,
				wave_id=wave_order_id or None,
			)
			return
		_push_inner(pe_name, wave_order_id, payment_status, correlation_id)
	except Exception as exc:
		log_step(
			correlation_id=correlation_id,
			step=STEP_UNEXPECTED_ERROR,
			level="Error",
			doc_type="Payment Entry",
			linked_doctype="Payment Entry",
			linked_docname=pe_name,
			wave_id=wave_order_id or None,
			error_message=f"unexpected exception in push_payment_status: {exc}",
			stack_trace=frappe.get_traceback(),
		)


def _push_inner(
	pe_name: str,
	wave_order_id: str,
	payment_status: str,
	correlation_id: str,
) -> None:
	settings = frappe.get_cached_doc("Wave Settings")
	config = resolve_outbound_config(settings)
	if config is None:
		log_step(
			correlation_id=correlation_id,
			step=STEP_ABORTED_MISSING_CONFIG,
			level="Error",
			doc_type="Payment Entry",
			linked_doctype="Payment Entry",
			linked_docname=pe_name,
			wave_id=wave_order_id,
			error_message="Wave outbound config incomplete (base_url / api_key / app_id).",
		)
		return

	body = {"paymentStatus": payment_status}
	url_path = f"/api/v3/admin/orders/{wave_order_id}"
	log_step(
		correlation_id=correlation_id,
		step=STEP_ATTEMPT,
		level="Info",
		doc_type="Payment Entry",
		linked_doctype="Payment Entry",
		linked_docname=pe_name,
		wave_id=wave_order_id,
		request_body={"method": "PATCH", "path": url_path, "body": body},
	)
	try:
		response = wave_client.patch_order_top_level(
			base_url=config["base_url"],
			api_key=config["api_key"],
			app_id=config["app_id"],
			order_id=wave_order_id,
			body=body,
		)
	except WaveOutboundError as exc:
		log_step(
			correlation_id=correlation_id,
			step=STEP_FAILED,
			level="Error",
			doc_type="Payment Entry",
			linked_doctype="Payment Entry",
			linked_docname=pe_name,
			wave_id=wave_order_id,
			request_body={"path": url_path, "body": body},
			error_message=str(exc),
			stack_trace=frappe.get_traceback(),
		)
		return

	actual = response.get("paymentStatus") if isinstance(response, dict) else None
	if actual != payment_status:
		log_step(
			correlation_id=correlation_id,
			step=STEP_RESPONSE_MISMATCH,
			level="Warning",
			doc_type="Payment Entry",
			linked_doctype="Payment Entry",
			linked_docname=pe_name,
			wave_id=wave_order_id,
			request_body={"path": url_path, "body": body},
			response_body=_summarise_response(response),
			error_message=(
				f"Wave acknowledged the PATCH (HTTP success) but its paymentStatus is "
				f"{actual!r}, not the {payment_status!r} we sent. Wave may have rejected the "
				"transition; verify the order on Wave before treating it as settled."
			),
		)
		return

	log_step(
		correlation_id=correlation_id,
		step=STEP_SUCCESS,
		level="Success",
		doc_type="Payment Entry",
		linked_doctype="Payment Entry",
		linked_docname=pe_name,
		wave_id=wave_order_id,
		request_body={"path": url_path, "body": body},
		response_body=_summarise_response(response),
	)


def _summarise_response(response: dict) -> dict:
	"""Headline fields only — full OrderV3 bloats audit rows."""
	if not isinstance(response, dict):
		return {"raw": response}
	return {
		"order_id": response.get("_id"),
		"status": response.get("status"),
		"payment_status": response.get("paymentStatus"),
		"updated_at": response.get("updatedAt"),
	}
