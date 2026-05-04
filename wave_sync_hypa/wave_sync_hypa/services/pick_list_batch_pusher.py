"""Worker job: PATCH a Wave order with the picked items' batch numbers.

Triggered by handlers.pick_list.after_pick_list_insert when
`pick_list_batch_ids_push_enabled` is on. The handler does the row-walking
and grouping (one job per linked Wave order, body containing only items
that have at least one batch_no), so this worker's responsibility is:

  1. Re-read Wave Settings (mid-queue kill-switch safety).
  2. Resolve each item_code to a Wave product _id via product_resolver
     (cached on Item.wave_product_id; falls back to Wave's by-sku endpoint).
  3. Build the minimal PATCH body — `{"products": [...]}` and nothing else,
     each product carrying productId + batchIds (deduped, encounter order).
  4. PATCH /api/v3/admin/orders/{wave_order_id}?skipWebhookNotification=false
     and log the outcome.

Top-level try/except wraps the body so an unexpected exception in one job
never breaks the worker loop. Same defensive pattern as stock_pusher and
order_status_pusher.
"""

from __future__ import annotations

import frappe

from wave_sync_hypa.wave_sync_hypa.services import product_resolver, wave_client
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveOutboundError

STEP_WORKER_STARTED = "pick_list_batch_ids_push_worker_started"
STEP_PUSH_ATTEMPT = "pick_list_batch_ids_push_attempt"
STEP_PUSH_SUCCESS = "pick_list_batch_ids_push_success"
STEP_PUSH_FAILED = "pick_list_batch_ids_push_failed"
STEP_ABORTED_DISABLED = "pick_list_batch_ids_push_aborted_disabled"
STEP_ABORTED_MISSING_CONFIG = "pick_list_batch_ids_push_aborted_missing_config"
STEP_ABORTED_NO_WAVE_ID = "pick_list_batch_ids_push_aborted_no_wave_order_id"
STEP_ABORTED_EMPTY_PAYLOAD = "pick_list_batch_ids_push_aborted_empty_payload"
STEP_SKIPPED_UNRESOLVED_ITEM = "pick_list_batch_ids_push_skipped_unresolved_item"
STEP_UNEXPECTED_ERROR = "pick_list_batch_ids_push_unexpected_error"


def push_pick_list_batch_ids(
	*,
	pick_list_name: str,
	wave_order_id: str,
	products_data: list[dict],
	correlation_id: str,
	manual_trigger: bool = False,
) -> None:
	"""Job entry point: PATCH the Wave order with batch numbers; never raises.

	`products_data` shape: [{"item_code": "...", "batch_ids": [...]}, ...].
	The handler pre-grouped these per Wave order; we resolve item_code ->
	wave_product_id here and then PATCH.

	`manual_trigger=True` bypasses the `pick_list_batch_ids_push_enabled`
	kill-switch — the operator-clicked "Send Batch IDs to Wave" button is
	an explicit consent and should fire regardless of the auto-on-create
	setting. The outbound config triplet (base_url/api_key/app_id) is
	still required either way.
	"""
	log_step(
		correlation_id=correlation_id,
		step=STEP_WORKER_STARTED,
		level="Info",
		doc_type="Pick List",
		linked_doctype="Pick List",
		linked_docname=pick_list_name,
		wave_id=wave_order_id or None,
		request_body={"manual_trigger": manual_trigger} if manual_trigger else None,
	)
	try:
		_push_inner(pick_list_name, wave_order_id, products_data, correlation_id, manual_trigger)
	except Exception as exc:
		log_step(
			correlation_id=correlation_id,
			step=STEP_UNEXPECTED_ERROR,
			level="Error",
			doc_type="Pick List",
			linked_doctype="Pick List",
			linked_docname=pick_list_name,
			wave_id=wave_order_id or None,
			error_message=f"unexpected exception in push_pick_list_batch_ids: {exc}",
			stack_trace=frappe.get_traceback(),
		)


def _push_inner(
	pick_list_name: str,
	wave_order_id: str,
	products_data: list[dict],
	correlation_id: str,
	manual_trigger: bool = False,
) -> None:
	"""Validate, resolve, build minimal body, PATCH; log every transition."""
	settings = frappe.get_cached_doc("Wave Settings")

	if not manual_trigger and not settings.get("pick_list_batch_ids_push_enabled"):
		log_step(
			correlation_id=correlation_id,
			step=STEP_ABORTED_DISABLED,
			level="Warning",
			doc_type="Pick List",
			linked_doctype="Pick List",
			linked_docname=pick_list_name,
			wave_id=wave_order_id or None,
			error_message="pick_list_batch_ids_push_enabled is off; skipping PATCH.",
		)
		return

	if not wave_order_id:
		log_step(
			correlation_id=correlation_id,
			step=STEP_ABORTED_NO_WAVE_ID,
			level="Warning",
			doc_type="Pick List",
			linked_doctype="Pick List",
			linked_docname=pick_list_name,
			error_message="Worker received empty wave_order_id; cannot PATCH.",
		)
		return

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

	products_payload = _build_products_payload(
		pick_list_name, wave_order_id, products_data, settings, correlation_id
	)
	if not products_payload:
		log_step(
			correlation_id=correlation_id,
			step=STEP_ABORTED_EMPTY_PAYLOAD,
			level="Warning",
			doc_type="Pick List",
			linked_doctype="Pick List",
			linked_docname=pick_list_name,
			wave_id=wave_order_id,
			error_message=(
				"All Pick List items either lacked batch numbers or could not be resolved "
				"to a Wave product id; nothing to PATCH."
			),
		)
		return

	body = {"products": products_payload}
	_attempt_patch(pick_list_name, wave_order_id, config, body, correlation_id)


def _build_products_payload(
	pick_list_name: str,
	wave_order_id: str,
	products_data: list[dict],
	settings,
	correlation_id: str,
) -> list[dict]:
	"""Resolve item_code -> wave_product_id for each entry; drop unresolved with a Warning."""
	out: list[dict] = []
	for entry in products_data or []:
		item_code = (entry.get("item_code") or "").strip()
		raw_batches = entry.get("batch_ids") or []
		batches = list(dict.fromkeys(b for b in raw_batches if b))
		if not item_code or not batches:
			continue
		wave_product_id = _get_or_resolve_wave_product_id(item_code, settings, correlation_id)
		if not wave_product_id:
			log_step(
				correlation_id=correlation_id,
				step=STEP_SKIPPED_UNRESOLVED_ITEM,
				level="Warning",
				doc_type="Pick List",
				linked_doctype="Pick List",
				linked_docname=pick_list_name,
				wave_id=wave_order_id,
				request_body={"item_code": item_code, "batch_ids": batches},
				error_message=(
					f"Could not resolve Wave product id for sku='{item_code}'; "
					"its batch ids will not be sent. See most recent product_resolve_* row."
				),
			)
			continue
		out.append({"productId": wave_product_id, "batchIds": batches})
	return out


def _attempt_patch(
	pick_list_name: str,
	wave_order_id: str,
	config: dict,
	body: dict,
	correlation_id: str,
) -> None:
	"""Issue the PATCH; log attempt + outcome."""
	url_path = f"/api/v3/admin/orders/{wave_order_id}"
	log_step(
		correlation_id=correlation_id,
		step=STEP_PUSH_ATTEMPT,
		level="Info",
		doc_type="Pick List",
		linked_doctype="Pick List",
		linked_docname=pick_list_name,
		wave_id=wave_order_id,
		request_body={"method": "PATCH", "path": url_path, "body": body},
	)
	try:
		response = wave_client.patch_order(
			base_url=config["base_url"],
			api_key=config["api_key"],
			app_id=config["app_id"],
			order_id=wave_order_id,
			body=body,
		)
	except WaveOutboundError as exc:
		log_step(
			correlation_id=correlation_id,
			step=STEP_PUSH_FAILED,
			level="Error",
			doc_type="Pick List",
			linked_doctype="Pick List",
			linked_docname=pick_list_name,
			wave_id=wave_order_id,
			request_body={"path": url_path, "body": body},
			error_message=str(exc),
			stack_trace=frappe.get_traceback(),
		)
		return

	# Wave's PATCH response is a full OrderV3 admin shape; log only the keys
	# operators usually want at a glance to keep audit rows compact.
	log_step(
		correlation_id=correlation_id,
		step=STEP_PUSH_SUCCESS,
		level="Success",
		doc_type="Pick List",
		linked_doctype="Pick List",
		linked_docname=pick_list_name,
		wave_id=wave_order_id,
		request_body={"path": url_path, "body": body},
		response_body=_summarise_response(response),
	)


def _summarise_response(response: dict) -> dict:
	"""Return only the headline fields from Wave's OrderV3 response (full body bloats logs)."""
	if not isinstance(response, dict):
		return {"raw": response}
	return {
		"order_id": response.get("_id"),
		"status": response.get("status"),
		"updated_at": response.get("updatedAt"),
	}


def _get_or_resolve_wave_product_id(item_code: str, settings, correlation_id: str) -> str | None:
	"""Return the cached Wave product id for an Item, resolving via Wave's by-sku endpoint on first use."""
	cached = frappe.db.get_value("Item", item_code, "wave_product_id")
	if cached:
		return cached
	return product_resolver.resolve_wave_product_id(item_code, settings, correlation_id)


def _resolve_outbound_config(settings) -> dict | None:
	"""Pull every value the HTTP call needs; return None if any required piece is missing."""
	base_url = (settings.get("wave_api_base_url") or "").strip()
	app_id = (settings.get("wave_app_id") or "").strip()
	api_key = settings.get_password("wave_api_key", raise_exception=False) or ""
	if not (base_url and app_id and api_key):
		return None
	return {"base_url": base_url, "app_id": app_id, "api_key": api_key}
