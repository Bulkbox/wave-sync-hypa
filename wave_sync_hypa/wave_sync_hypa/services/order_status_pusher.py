"""Worker job: POST a resolved status transition to Wave for one ERP source doc.

Wave's status endpoint is path-keyed:

    POST /api/v3/admin/orders/{order_id}/status/{status_name}

There is no body, no query string, and no equivalent endpoint for
deliveryStatus yet. The resolver may emit a payload carrying
{"status": "...", "deliveryStatus": "..."} based on rule rows; this
worker pushes the status component via the supported endpoint and logs
+ skips the deliveryStatus component until Wave provides its endpoint.

Source-doc identity (`source_doctype` + `source_docname`) is plumbed in
from the dispatcher so the audit log rows correctly point to the
triggering ERP document — Sales Order on SO submit/cancel, Delivery Note
on DN submit, Sales Invoice on SI / credit-note submit. Without this,
the Dynamic Link on Wave Sync Log fails validation when the source
isn't a Sales Order, which silently swallows the entire dispatch.

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
from wave_sync_hypa.wave_sync_hypa.services.master_switch import (
	STEP_MASTER_DISABLED,
	is_wave_integration_enabled,
)
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveOutboundError

STEP_PUSH_ATTEMPT = "order_status_push_attempt"
STEP_PUSH_SUCCESS = "order_status_push_success"
STEP_PUSH_FAILED = "order_status_push_failed"
STEP_PUSH_SKIPPED_TERMINAL = "order_status_push_skipped_terminal"
STEP_PUSH_ABORTED_DISABLED = "order_status_push_aborted_settings_off"
STEP_PUSH_ABORTED_MISSING_CONFIG = "order_status_push_aborted_missing_config"
STEP_PUSH_ABORTED_NO_WAVE_ID = "order_status_push_aborted_no_wave_order_id"
STEP_PUSH_ABORTED_EMPTY_PAYLOAD = "order_status_push_aborted_empty_payload"
STEP_PUSH_DELIVERY_STATUS_UNSUPPORTED = "order_status_push_delivery_status_unsupported"
STEP_PUSH_UNEXPECTED_ERROR = "order_status_push_unexpected_error"
STEP_WORKER_STARTED = "order_status_push_worker_started"

# Wave application-level error codes that mean "the order moved past the
# state we tried to set on it" (or "you can't act on this terminal order").
# Both are emitted by Wave on legitimate ERP-side amend / re-cancel flows
# where ERP fires submit/cancel a second time on an order Wave already
# considers settled. They are NOT actionable bugs; logging them at Error
# level was masking real failures behind ambient noise. We classify them
# as Warning + STEP_PUSH_SKIPPED_TERMINAL so dashboards can filter them
# out without losing the audit trail.
WAVE_TERMINAL_TRANSITION_CODES = frozenset(
	{
		# ORDER0049: "You cannot change the order status to the desired status" —
		# Wave rejects the transition because the current state forbids it (e.g.
		# push ACCEPTED on an order Wave already moved to UNDER_PICKING).
		# ORDER0034 used to be in this set on the assumption it meant "already
		# finalised", but live tracing on dev showed it's actually a real auth
		# failure ("you are not authorized to access this order") and the v3.1
		# admin/reject path never returns it. Surfacing as Error now.
		"ORDER0049",
	}
)

STEP_PUSH_CANCEL_REFUSED_PREPAID = "order_status_push_cancel_refused_prepaid"
STEP_CANCEL_CLEARED_BANNERS = "order_status_cancel_cleared_banners"

# On a confirmed SO cancel, silence the red banner flags. Everything else
# (wave_order_id, friendly_id, status, correlation_id, payment_*, ...) stays
# on the cancelled SO as an audit breadcrumb. Amend's fresh-Wave-order
# behaviour is guaranteed by no_copy=1 on every wave_* custom field, NOT by
# this clear pass.
_BANNER_FIELDS_TO_CLEAR_ON_CANCEL = (
	"wave_manual_review_required",
	"wave_push_failure_required_review",
)


def push_order_status(
	source_doctype: str = "Sales Order",
	source_docname: str = "",
	erp_event: str = "",
	payload: dict | None = None,
	correlation_id: str = "",
	wave_order_id: str = "",
	# Back-compat: older queued jobs from Phase 5 used `sales_order_name`.
	# Any in-flight job at the moment of deploy will land here and keep working.
	sales_order_name: str | None = None,
) -> None:
	"""Job entry point: POST the resolved status transition to Wave; never raises.

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
	if sales_order_name and not source_docname:
		source_docname = sales_order_name
	if payload is None:
		payload = {}

	log_step(
		correlation_id=correlation_id,
		step=STEP_WORKER_STARTED,
		level="Info",
		doc_type=source_doctype,
		action=erp_event,
		linked_doctype=source_doctype,
		linked_docname=source_docname,
		wave_id=wave_order_id or None,
	)
	try:
		# Master kill switch check sits INSIDE the try/except so that a
		# failure reading Wave Settings (transient DB / cache fault) is
		# absorbed by the same defensive log_unexpected_error path that
		# wraps every other line of _push_inner, preserving the worker's
		# never-raise contract.
		if not is_wave_integration_enabled():
			log_step(
				correlation_id=correlation_id,
				step=STEP_MASTER_DISABLED,
				level="Info",
				doc_type=source_doctype,
				action=erp_event,
				linked_doctype=source_doctype,
				linked_docname=source_docname,
				wave_id=wave_order_id or None,
			)
			return
		_push_inner(source_doctype, source_docname, erp_event, payload, correlation_id, wave_order_id)
	except Exception as exc:
		log_step(
			correlation_id=correlation_id,
			step=STEP_PUSH_UNEXPECTED_ERROR,
			level="Error",
			doc_type=source_doctype,
			action=erp_event,
			linked_doctype=source_doctype,
			linked_docname=source_docname,
			wave_id=wave_order_id or None,
			error_message=f"unexpected exception in push_order_status: {exc}",
			stack_trace=frappe.get_traceback(),
		)


def _push_inner(
	source_doctype: str,
	source_docname: str,
	erp_event: str,
	payload: dict,
	correlation_id: str,
	wave_order_id: str,
) -> None:
	"""Real work: validate config, POST status, log transitions.

	wave_order_id is plumbed from the dispatcher; we no longer re-look it up
	from a Sales Order. That re-lookup was wrong for DN/SI dispatches (the
	worker would query `Sales Order` keyed on a DN/SI name and get None),
	and is unnecessary for SO dispatches because the dispatcher already
	read the same value before enqueueing.
	"""
	settings = frappe.get_cached_doc("Wave Settings")

	if not settings.get("outbound_order_status_sync_enabled"):
		log_step(
			correlation_id=correlation_id,
			step=STEP_PUSH_ABORTED_DISABLED,
			level="Warning",
			doc_type=source_doctype,
			action=erp_event,
			linked_doctype=source_doctype,
			linked_docname=source_docname,
			wave_id=wave_order_id or None,
			error_message="outbound_order_status_sync_enabled is off; skipping push.",
		)
		return

	if not wave_order_id:
		log_step(
			correlation_id=correlation_id,
			step=STEP_PUSH_ABORTED_NO_WAVE_ID,
			level="Warning",
			doc_type=source_doctype,
			action=erp_event,
			linked_doctype=source_doctype,
			linked_docname=source_docname,
			error_message="Worker received empty wave_order_id; cannot push.",
		)
		return

	config = _resolve_outbound_config(settings)
	if config is None:
		log_step(
			correlation_id=correlation_id,
			step=STEP_PUSH_ABORTED_MISSING_CONFIG,
			level="Error",
			doc_type=source_doctype,
			action=erp_event,
			linked_doctype=source_doctype,
			linked_docname=source_docname,
			wave_id=wave_order_id,
			error_message="Wave outbound config incomplete (base_url / api_key / app_id).",
		)
		return

	_warn_if_delivery_status_present(
		payload, source_doctype, source_docname, erp_event, correlation_id, wave_order_id
	)

	status_name = (payload or {}).get("status")
	if not status_name:
		# After deliveryStatus-only rules are warned about, there's nothing supported left to push.
		log_step(
			correlation_id=correlation_id,
			step=STEP_PUSH_ABORTED_EMPTY_PAYLOAD,
			level="Info",
			doc_type=source_doctype,
			action=erp_event,
			linked_doctype=source_doctype,
			linked_docname=source_docname,
			wave_id=wave_order_id,
			error_message="No supported field in resolved payload (status missing).",
			request_body={"resolved_payload": payload},
		)
		return

	# Sales Order cancel routes through Wave's v3.1 admin/reject endpoint
	# (the only path authorized for our admin token on Wave-originated orders).
	# Other status transitions and other doctypes' cancels still use v3 status.
	if (
		source_doctype == "Sales Order"
		and erp_event == "cancel"
		and (status_name or "").upper() == "CANCELLED"
	):
		_post_cancel_via_reject(source_doctype, source_docname, erp_event, correlation_id, wave_order_id, config)
		return

	_post_status(source_doctype, source_docname, erp_event, correlation_id, wave_order_id, config, status_name)


def _post_status(
	source_doctype: str,
	source_docname: str,
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
		doc_type=source_doctype,
		action=erp_event,
		linked_doctype=source_doctype,
		linked_docname=source_docname,
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
		# Wave rejected the transition. Two flavours:
		#   - ORDER0049 "you cannot change the order status to the desired
		#     status": Wave's state machine refuses the transition because
		#     the order is already past it (e.g. amend re-pushes ACCEPTED on
		#     an already-UNDER_PICKING order). Not a bug; log Warning +
		#     STEP_PUSH_SKIPPED_TERMINAL so dashboards aren't pinged by
		#     routine amend / re-cancel noise.
		#   - Anything else (auth, 5xx, network, unknown 422): real failure,
		#     log Error + STEP_PUSH_FAILED so the team sees it. ORDER0034
		#     "you are not authorized to access this order" used to be in
		#     the terminal set under the assumption it meant "already
		#     finalised"; live tracing on dev showed it's a real auth
		#     failure and now surfaces as Error.
		is_terminal = exc.wave_code in WAVE_TERMINAL_TRANSITION_CODES
		log_step(
			correlation_id=correlation_id,
			step=STEP_PUSH_SKIPPED_TERMINAL if is_terminal else STEP_PUSH_FAILED,
			level="Warning" if is_terminal else "Error",
			doc_type=source_doctype,
			action=erp_event,
			linked_doctype=source_doctype,
			linked_docname=source_docname,
			wave_id=wave_order_id,
			request_body={"path": url_path, "status_name": status_name},
			error_message=str(exc),
			stack_trace=None if is_terminal else frappe.get_traceback(),
		)
		return

	log_step(
		correlation_id=correlation_id,
		step=STEP_PUSH_SUCCESS,
		level="Success",
		doc_type=source_doctype,
		action=erp_event,
		linked_doctype=source_doctype,
		linked_docname=source_docname,
		wave_id=wave_order_id,
		request_body={"path": url_path, "status_name": status_name},
		response_body=response,
	)


def _post_cancel_via_reject(
	source_doctype: str,
	source_docname: str,
	erp_event: str,
	correlation_id: str,
	wave_order_id: str,
	config: dict,
) -> None:
	"""POST /api/v3.1/admin/orders/{id}/reject. On 200, clear ERP wave fields."""
	url_path = f"/api/v3.1/admin/orders/{wave_order_id}/reject"
	log_step(
		correlation_id=correlation_id,
		step=STEP_PUSH_ATTEMPT,
		level="Info",
		doc_type=source_doctype,
		action=erp_event,
		linked_doctype=source_doctype,
		linked_docname=source_docname,
		wave_id=wave_order_id,
		request_body={"method": "POST", "path": url_path, "order_id": wave_order_id},
	)
	try:
		response = wave_client.reject_admin_order(
			base_url=config["base_url"],
			api_key=config["api_key"],
			app_id=config["app_id"],
			order_id=wave_order_id,
		)
	except WaveOutboundError as exc:
		# ORDER0005 "the order cannot be cancelled" — Wave business rule
		# (prepaid orders). Real refusal: do NOT clear banners, the link is
		# still live on Wave and the operator needs to reconcile manually.
		#
		# ORDER0049 state-machine refusal — Wave reports the order is
		# already terminal. From ERP's perspective the cancel intent has
		# effectively been achieved (the order on Wave is already gone),
		# so we DO clear the banner flags as if it were a 200.
		is_prepaid_refusal = exc.wave_code == "ORDER0005"
		is_terminal = exc.wave_code in WAVE_TERMINAL_TRANSITION_CODES
		if is_prepaid_refusal:
			step, level = STEP_PUSH_CANCEL_REFUSED_PREPAID, "Warning"
		elif is_terminal:
			step, level = STEP_PUSH_SKIPPED_TERMINAL, "Warning"
		else:
			step, level = STEP_PUSH_FAILED, "Error"
		log_step(
			correlation_id=correlation_id,
			step=step,
			level=level,
			doc_type=source_doctype,
			action=erp_event,
			linked_doctype=source_doctype,
			linked_docname=source_docname,
			wave_id=wave_order_id,
			request_body={"path": url_path},
			error_message=str(exc),
			stack_trace=None if (is_prepaid_refusal or is_terminal) else frappe.get_traceback(),
		)
		if is_terminal and not is_prepaid_refusal:
			_clear_so_banner_flags_on_cancel(source_docname, correlation_id, wave_order_id)
		return

	log_step(
		correlation_id=correlation_id,
		step=STEP_PUSH_SUCCESS,
		level="Success",
		doc_type=source_doctype,
		action=erp_event,
		linked_doctype=source_doctype,
		linked_docname=source_docname,
		wave_id=wave_order_id,
		request_body={"path": url_path},
		response_body={
			"_id": response.get("_id"),
			"friendlyId": response.get("friendlyId"),
			"cancelType": response.get("cancelType"),
		},
	)
	_clear_so_banner_flags_on_cancel(source_docname, correlation_id, wave_order_id)


def _clear_so_banner_flags_on_cancel(
	so_name: str,
	correlation_id: str,
	wave_order_id: str,
) -> None:
	"""Zero the two Check banner flags on the cancelled SO in a single UPDATE.

	A cancelled SO should not display the red 'manual review' / 'push failure'
	banners. Every other wave_* field stays for audit; amend safety is handled
	by no_copy=1 on the custom field definitions.
	"""
	frappe.db.set_value(
		"Sales Order",
		so_name,
		{field: 0 for field in _BANNER_FIELDS_TO_CLEAR_ON_CANCEL},
		update_modified=False,
	)
	log_step(
		correlation_id=correlation_id,
		step=STEP_CANCEL_CLEARED_BANNERS,
		level="Info",
		doc_type="Sales Order",
		action="cancel",
		linked_doctype="Sales Order",
		linked_docname=so_name,
		wave_id=wave_order_id,
		response_body={"cleared_fields": list(_BANNER_FIELDS_TO_CLEAR_ON_CANCEL)},
	)


def _warn_if_delivery_status_present(
	payload: dict,
	source_doctype: str,
	source_docname: str,
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
		doc_type=source_doctype,
		action=erp_event,
		linked_doctype=source_doctype,
		linked_docname=source_docname,
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
