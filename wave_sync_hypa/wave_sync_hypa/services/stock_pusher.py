"""Worker-side stock-push job: read current ERP stock and POST it to Wave.

Runs in the RQ worker via `frappe.enqueue`. Re-reads Wave Settings on every
invocation so a kill-switch flip takes effect mid-queue. Reads the current
Bin quantity (not the SLE delta) because Wave's `/stock/sync` endpoint is
absolute, not incremental — so even if many SLEs collapsed into one queued
job, we always push the latest known balance.

Every decision and HTTP outcome is logged to Wave Sync Log so operators can
trace any item's stock-push history end to end via correlation_id. Manual
resyncs additionally pass a batch_id which is stamped into friendly_id on
every log row so an entire run is filterable with one query.

The whole body is wrapped in a defensive try/except. The contract with
callers (handler enqueue, resync coordinator) is: this function never raises.
A buggy or malformed payload for one item must not break the worker loop.
"""

from __future__ import annotations

import frappe

from wave_sync_hypa.wave_sync_hypa.services import wave_client
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveOutboundError

STEP_PUSH_ATTEMPT = "stock_sync_push_attempt"
STEP_PUSH_SUCCESS = "stock_sync_push_success"
STEP_PUSH_FAILED = "stock_sync_push_failed"
STEP_PUSH_ABORTED_DISABLED = "stock_sync_push_aborted_settings_off"
STEP_PUSH_ABORTED_NO_WAREHOUSE = "stock_sync_push_aborted_no_default_warehouse"
STEP_PUSH_ABORTED_MISSING_CONFIG = "stock_sync_push_aborted_missing_config"
STEP_PUSH_UNEXPECTED_ERROR = "stock_sync_push_unexpected_error"


def push_item_stock(item_code: str, correlation_id: str, batch_id: str | None = None) -> None:
	"""Job entry point: push current default-warehouse qty for one item to Wave; never raises."""
	try:
		_push_item_stock_inner(item_code, correlation_id, batch_id)
	except Exception as exc:
		log_step(
			correlation_id=correlation_id,
			step=STEP_PUSH_UNEXPECTED_ERROR,
			level="Error",
			doc_type="Item",
			linked_doctype="Item",
			linked_docname=item_code,
			friendly_id=batch_id,
			error_message=f"unexpected exception in push_item_stock: {exc}",
			stack_trace=frappe.get_traceback(),
		)


def _push_item_stock_inner(item_code: str, correlation_id: str, batch_id: str | None) -> None:
	"""Real work: validate config, read Bin, call Wave, log every transition."""
	settings = frappe.get_cached_doc("Wave Settings")

	if not settings.get("outbound_stock_sync_enabled"):
		log_step(
			correlation_id=correlation_id,
			step=STEP_PUSH_ABORTED_DISABLED,
			level="Warning",
			doc_type="Item",
			linked_doctype="Item",
			linked_docname=item_code,
			friendly_id=batch_id,
			error_message="outbound_stock_sync_enabled is off; skipping push.",
		)
		return

	warehouse = settings.get("default_warehouse")
	if not warehouse:
		log_step(
			correlation_id=correlation_id,
			step=STEP_PUSH_ABORTED_NO_WAREHOUSE,
			level="Error",
			doc_type="Item",
			linked_doctype="Item",
			linked_docname=item_code,
			friendly_id=batch_id,
			error_message="Wave Settings.default_warehouse is not set.",
		)
		return

	config = _resolve_outbound_config(settings)
	if config is None:
		log_step(
			correlation_id=correlation_id,
			step=STEP_PUSH_ABORTED_MISSING_CONFIG,
			level="Error",
			doc_type="Item",
			linked_doctype="Item",
			linked_docname=item_code,
			friendly_id=batch_id,
			error_message="Wave outbound config incomplete (base_url / api_key / app_id / store_id).",
		)
		return

	quantity = _current_default_warehouse_qty(item_code, warehouse)
	body = {"productId": item_code, "storeId": config["store_id"], "quantity": quantity}

	log_step(
		correlation_id=correlation_id,
		step=STEP_PUSH_ATTEMPT,
		level="Info",
		doc_type="Item",
		linked_doctype="Item",
		linked_docname=item_code,
		friendly_id=batch_id,
		request_body=body,
	)

	try:
		response = wave_client.post_stock_sync(
			base_url=config["base_url"],
			api_key=config["api_key"],
			app_id=config["app_id"],
			product_id=item_code,
			store_id=config["store_id"],
			quantity=quantity,
		)
	except WaveOutboundError as exc:
		log_step(
			correlation_id=correlation_id,
			step=STEP_PUSH_FAILED,
			level="Error",
			doc_type="Item",
			linked_doctype="Item",
			linked_docname=item_code,
			friendly_id=batch_id,
			request_body=body,
			error_message=str(exc),
			stack_trace=frappe.get_traceback(),
		)
		return

	log_step(
		correlation_id=correlation_id,
		step=STEP_PUSH_SUCCESS,
		level="Info",
		doc_type="Item",
		linked_doctype="Item",
		linked_docname=item_code,
		friendly_id=batch_id,
		request_body=body,
		response_body=response,
	)


def _resolve_outbound_config(settings) -> dict | None:
	"""Pull every value the HTTP call needs; return None if any required piece is missing."""
	base_url = (settings.get("wave_api_base_url") or "").strip()
	app_id = (settings.get("wave_app_id") or "").strip()
	store_id = (settings.get("wave_store_id") or "").strip()
	api_key = settings.get_password("wave_api_key", raise_exception=False) or ""
	if not (base_url and app_id and store_id and api_key):
		return None
	return {"base_url": base_url, "app_id": app_id, "store_id": store_id, "api_key": api_key}


def _current_default_warehouse_qty(item_code: str, warehouse: str) -> int:
	"""Return Bin.actual_qty for (item, warehouse) as an int; clamp negatives and missing rows to 0.

	Items with no Bin row (never moved in this warehouse) read as None and become 0.
	Negative balances (transient oversold state) also become 0 — we never report
	negative stock to Wave, since the storefront cannot meaningfully act on it.
	"""
	actual = frappe.db.get_value(
		"Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"
	)
	qty = int(round(actual or 0))
	return qty if qty > 0 else 0
