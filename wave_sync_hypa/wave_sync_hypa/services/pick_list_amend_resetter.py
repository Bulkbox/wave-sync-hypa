"""EXPERIMENTAL — Reset Wave's picker state when an ERP Pick List is amended.

================================================================
                        READ ME BEFORE EDITING
================================================================
This whole module is the experimental fix for issue #113: a cancelled +
amended ERP Pick List leaves the matching Wave order with
`pickerStatus = "COLLECTED"` and a populated `picking` subtree, so the
picker app filters the amended order out of its queue and pickers cannot
re-pick. The reset PATCHes `/admin/orders/{id}` (top-level fields, NOT the
products array) with a body that nulls every picker field in one call.

To FULLY REVERT this feature (three deletions, no other changes):
  1. delete this file
  2. delete the EXPERIMENTAL fenced block in
     handlers/pick_list.after_pick_list_insert
  3. delete wave_client.patch_order_top_level (unused without this module)

Worker pattern matches pick_list_batch_pusher: master switch checked
inside try/except, never raises, every transition audited via log_step.
================================================================
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
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveOutboundError

WORKER_DOTTED_PATH = (
	"wave_sync_hypa.wave_sync_hypa.services.pick_list_amend_resetter.reset_picker_state"
)

STEP_ENQUEUED = "pick_list_amend_picker_reset_enqueued"
STEP_ENQUEUE_FAILED = "pick_list_amend_picker_reset_enqueue_failed"
STEP_WORKER_STARTED = "pick_list_amend_picker_reset_worker_started"
STEP_ATTEMPT = "pick_list_amend_picker_reset_attempt"
STEP_SUCCESS = "pick_list_amend_picker_reset_success"
STEP_RESPONSE_MISMATCH = "pick_list_amend_picker_reset_response_mismatch"
STEP_FAILED = "pick_list_amend_picker_reset_failed"
STEP_ABORTED_MISSING_CONFIG = "pick_list_amend_picker_reset_aborted_missing_config"
STEP_UNEXPECTED_ERROR = "pick_list_amend_picker_reset_unexpected_error"

# The exact body the worker sends. Single dict, two keys, both null —
# trusting Wave to recursively wipe the `picking` subtree (completedAt,
# assignedAt, assignedTo*, items[*].status) when it sees `picking: null`.
# If a live trace shows Wave rejects this shape, swap for a hand-built
# nested dict here — no caller changes needed.
PICKER_STATE_RESET_BODY: dict = {
	"pickerStatus": None,
	"picking": None,
}


def enqueue_picker_state_reset(doc, wave_order_ids: list[str], settings) -> None:
	"""Entry point for the handler: one async worker per Wave order id.

	Caller (handlers.pick_list.after_pick_list_insert) gates this on
	`doc.amended_from` — the worker itself doesn't re-check, so this
	function is safe to invoke only from an amend context.
	"""
	correlation_id = new_correlation_id()
	for wave_order_id in wave_order_ids:
		try:
			frappe.enqueue(
				WORKER_DOTTED_PATH,
				queue="default",
				enqueue_after_commit=True,
				job_name=f"pick_list_amend_picker_reset:{doc.name}:{wave_order_id}",
				pick_list_name=doc.name,
				wave_order_id=wave_order_id,
				correlation_id=correlation_id,
			)
		except Exception as exc:
			log_step(
				correlation_id=correlation_id,
				step=STEP_ENQUEUE_FAILED,
				level="Error",
				doc_type=doc.doctype,
				linked_doctype=doc.doctype,
				linked_docname=doc.name,
				wave_id=wave_order_id,
				error_message=f"failed to enqueue picker-state reset: {exc}",
				stack_trace=frappe.get_traceback(),
			)
			continue
		log_step(
			correlation_id=correlation_id,
			step=STEP_ENQUEUED,
			level="Info",
			doc_type=doc.doctype,
			linked_doctype=doc.doctype,
			linked_docname=doc.name,
			wave_id=wave_order_id,
		)


def reset_picker_state(
	*,
	pick_list_name: str,
	wave_order_id: str,
	correlation_id: str,
) -> None:
	"""Worker job: PATCH the Wave order's top-level picker fields to null. Never raises."""
	log_step(
		correlation_id=correlation_id,
		step=STEP_WORKER_STARTED,
		level="Info",
		doc_type="Pick List",
		linked_doctype="Pick List",
		linked_docname=pick_list_name,
		wave_id=wave_order_id or None,
	)
	try:
		if not is_wave_integration_enabled():
			log_step(
				correlation_id=correlation_id,
				step=STEP_MASTER_DISABLED,
				level="Info",
				doc_type="Pick List",
				linked_doctype="Pick List",
				linked_docname=pick_list_name,
				wave_id=wave_order_id or None,
			)
			return
		_reset_inner(pick_list_name, wave_order_id, correlation_id)
	except Exception as exc:
		log_step(
			correlation_id=correlation_id,
			step=STEP_UNEXPECTED_ERROR,
			level="Error",
			doc_type="Pick List",
			linked_doctype="Pick List",
			linked_docname=pick_list_name,
			wave_id=wave_order_id or None,
			error_message=f"unexpected exception in reset_picker_state: {exc}",
			stack_trace=frappe.get_traceback(),
		)


def _reset_inner(pick_list_name: str, wave_order_id: str, correlation_id: str) -> None:
	settings = frappe.get_cached_doc("Wave Settings")
	config = _resolve_outbound_config(settings)
	if config is None:
		log_step(
			correlation_id=correlation_id,
			step=STEP_ABORTED_MISSING_CONFIG,
			level="Error",
			doc_type="Pick List",
			linked_doctype="Pick List",
			linked_docname=pick_list_name,
			wave_id=wave_order_id,
			error_message="Wave outbound config incomplete (base_url / api_key / app_id).",
		)
		return

	url_path = f"/api/v3/admin/orders/{wave_order_id}"
	log_step(
		correlation_id=correlation_id,
		step=STEP_ATTEMPT,
		level="Info",
		doc_type="Pick List",
		linked_doctype="Pick List",
		linked_docname=pick_list_name,
		wave_id=wave_order_id,
		request_body={"method": "PATCH", "path": url_path, "body": PICKER_STATE_RESET_BODY},
	)
	try:
		response = wave_client.patch_order_top_level(
			base_url=config["base_url"],
			api_key=config["api_key"],
			app_id=config["app_id"],
			order_id=wave_order_id,
			body=PICKER_STATE_RESET_BODY,
		)
	except WaveOutboundError as exc:
		log_step(
			correlation_id=correlation_id,
			step=STEP_FAILED,
			level="Error",
			doc_type="Pick List",
			linked_doctype="Pick List",
			linked_docname=pick_list_name,
			wave_id=wave_order_id,
			request_body={"path": url_path, "body": PICKER_STATE_RESET_BODY},
			error_message=str(exc),
			stack_trace=frappe.get_traceback(),
		)
		return

	leftover = _residual_picker_state(response)
	if leftover:
		log_step(
			correlation_id=correlation_id,
			step=STEP_RESPONSE_MISMATCH,
			level="Warning",
			doc_type="Pick List",
			linked_doctype="Pick List",
			linked_docname=pick_list_name,
			wave_id=wave_order_id,
			request_body={"path": url_path, "body": PICKER_STATE_RESET_BODY},
			response_body=_summarise_response(response),
			error_message=(
				"Wave acknowledged the PATCH (HTTP success) but the picker state is still "
				f"populated after the reset: {leftover}. The picker app may keep filtering the "
				"amended order out; verify the order on Wave."
			),
		)
		return

	log_step(
		correlation_id=correlation_id,
		step=STEP_SUCCESS,
		level="Success",
		doc_type="Pick List",
		linked_doctype="Pick List",
		linked_docname=pick_list_name,
		wave_id=wave_order_id,
		request_body={"path": url_path, "body": PICKER_STATE_RESET_BODY},
		response_body=_summarise_response(response),
	)


def _residual_picker_state(response) -> dict:
	"""Return the picker fields Wave left populated after the null reset, or {} if clean.

	This is a *negative* confirmation (we expect both fields to come back
	empty), so a body we can't recognise as a real order echo must count as
	"could not verify" — otherwise a garbage 2xx body (e.g. wave_client's
	`{"raw": ...}` parse-failure wrapper, which simply lacks the picker keys)
	would read as a clean reset. We use `_id` as the order-echo marker, the
	same signal wave_client uses to validate a 2xx product body.
	"""
	if not isinstance(response, dict) or not response.get("_id"):
		return {"response": "unrecognised order body; reset could not be verified"}
	out: dict = {}
	if response.get("pickerStatus"):
		out["pickerStatus"] = response.get("pickerStatus")
	if response.get("picking"):
		out["picking"] = "<still populated>"
	return out


def _summarise_response(response: dict) -> dict:
	"""Headline fields only — full OrderV3 bloats audit rows."""
	if not isinstance(response, dict):
		return {"raw": response}
	return {
		"order_id": response.get("_id"),
		"status": response.get("status"),
		"picker_status": response.get("pickerStatus"),
		"picking": response.get("picking"),
		"updated_at": response.get("updatedAt"),
	}


def _resolve_outbound_config(settings) -> dict | None:
	base_url = (settings.get("wave_api_base_url") or "").strip()
	app_id = (settings.get("wave_app_id") or "").strip()
	api_key = settings.get_password("wave_api_key", raise_exception=False) or ""
	if not (base_url and app_id and api_key):
		return None
	return {"base_url": base_url, "app_id": app_id, "api_key": api_key}
