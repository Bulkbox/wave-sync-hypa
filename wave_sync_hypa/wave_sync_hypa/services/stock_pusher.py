"""Worker-side stock-push job: read current ERP stock and POST it to Wave.

Runs in the RQ worker via `frappe.enqueue`. Re-reads Wave Settings on every
invocation so a kill-switch flip takes effect mid-queue. Reads the current
Bin quantity (not the SLE delta) because Wave's `/stock/sync` endpoint is
absolute, not incremental — so even if many SLEs collapsed into one queued
job, we always push the latest known balance.

Wave keys stock requests on its internal product `_id`, not on sku, so the
first thing this job does is translate `Item.item_code` → Wave `_id` via
the product_resolver service. The resolved id is cached on
`Item.wave_product_id` so steady-state pushes skip the lookup. If Wave
later returns PRODUCT0006 ("product with id not found"), the cached id is
stale — the product was deleted and recreated under a new `_id` — and we
re-resolve once before retrying.

Every decision and HTTP outcome is logged to Wave Sync Log so operators
can trace any item's stock-push history end to end via correlation_id.
Manual resyncs additionally pass a batch_id which is stamped into
friendly_id on every log row so an entire run is filterable with one query.

The whole body is wrapped in a defensive try/except. The contract with
callers (handler enqueue, resync coordinator) is: this function never raises.
A buggy or malformed payload for one item must not break the worker loop.
"""

from __future__ import annotations

import frappe

from wave_sync_hypa.wave_sync_hypa.services import product_resolver, wave_client
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveOutboundError

STEP_PUSH_ATTEMPT = "stock_sync_push_attempt"
STEP_PUSH_SUCCESS = "stock_sync_push_success"
STEP_PUSH_FAILED = "stock_sync_push_failed"
STEP_PUSH_RETRY_AFTER_RESOLVE = "stock_sync_push_retry_after_resolve"
STEP_PUSH_ABORTED_DISABLED = "stock_sync_push_aborted_settings_off"
STEP_PUSH_ABORTED_NO_WAREHOUSE = "stock_sync_push_aborted_no_default_warehouse"
STEP_PUSH_ABORTED_MISSING_CONFIG = "stock_sync_push_aborted_missing_config"
STEP_PUSH_ABORTED_UNMAPPED = "stock_sync_push_aborted_product_unmapped"
STEP_PUSH_UNEXPECTED_ERROR = "stock_sync_push_unexpected_error"

# Wave's app-level error code for "product with the given _id not found".
# Treated as a signal to refresh our cached wave_product_id and retry once.
WAVE_CODE_PRODUCT_NOT_FOUND = "PRODUCT0006"


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
	"""Real work: validate config, resolve Wave id, read Bin, POST, retry on PRODUCT0006."""
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

	wave_product_id = _get_or_resolve_wave_product_id(item_code, settings, correlation_id)
	if not wave_product_id:
		log_step(
			correlation_id=correlation_id,
			step=STEP_PUSH_ABORTED_UNMAPPED,
			level="Error",
			doc_type="Item",
			linked_doctype="Item",
			linked_docname=item_code,
			friendly_id=batch_id,
			error_message=(
				f"Could not resolve Wave product id for sku='{item_code}'. "
				"See most recent product_resolve_* row for the underlying cause."
			),
		)
		return

	quantity = _current_default_warehouse_qty(item_code, warehouse)
	# Mirror the JSON body that wave_client.post_stock_sync builds internally so
	# every log row's request_body matches what actually went over the wire.
	# Wave's spec requires productId in the body to be the Wave-side `_id`,
	# matching the {id} path param — sending item_code here would diverge
	# from the live request and confuse operators reading the audit trail.
	body = {"productId": wave_product_id, "storeId": config["store_id"], "quantity": quantity}

	if not _attempt_push(item_code, wave_product_id, config, body, correlation_id, batch_id):
		# First push failed with PRODUCT0006: refresh cached id and retry once.
		# We DO NOT trust the previously cached id at this point — _attempt_push
		# only returns False when the cached id was definitively rejected by Wave.
		fresh_id = product_resolver.resolve_wave_product_id(item_code, settings, correlation_id)
		if not fresh_id or fresh_id == wave_product_id:
			# Resolver couldn't find the product OR returned the same id we
			# already tried. Either way, the original error row already
			# captured the failure; nothing more to do.
			return
		log_step(
			correlation_id=correlation_id,
			step=STEP_PUSH_RETRY_AFTER_RESOLVE,
			level="Warning",
			doc_type="Item",
			linked_doctype="Item",
			linked_docname=item_code,
			wave_id=fresh_id,
			friendly_id=batch_id,
			request_body={"previous_wave_id": wave_product_id, "new_wave_id": fresh_id},
			error_message="cached Wave product id was stale; retrying push with refreshed id.",
		)
		_attempt_push(item_code, fresh_id, config, body, correlation_id, batch_id)


def _attempt_push(
	item_code: str,
	wave_product_id: str,
	config: dict,
	body: dict,
	correlation_id: str,
	batch_id: str | None,
) -> bool:
	"""Issue one POST to Wave's stock/sync endpoint; return False ONLY for PRODUCT0006.

	Why a boolean instead of a richer return: the caller only branches on
	"should I re-resolve and retry?" and the only Wave error that warrants
	that branch is PRODUCT0006. Every other failure (auth, network, 5xx,
	unrelated 4xx) is logged here and consumed; the caller does not retry
	on those because retrying would not change the outcome.
	"""
	log_step(
		correlation_id=correlation_id,
		step=STEP_PUSH_ATTEMPT,
		level="Info",
		doc_type="Item",
		linked_doctype="Item",
		linked_docname=item_code,
		wave_id=wave_product_id,
		friendly_id=batch_id,
		request_body=body,
	)
	try:
		response = wave_client.post_stock_sync(
			base_url=config["base_url"],
			api_key=config["api_key"],
			app_id=config["app_id"],
			product_id=wave_product_id,
			store_id=config["store_id"],
			quantity=body["quantity"],
		)
	except WaveOutboundError as exc:
		log_step(
			correlation_id=correlation_id,
			step=STEP_PUSH_FAILED,
			level="Error",
			doc_type="Item",
			linked_doctype="Item",
			linked_docname=item_code,
			wave_id=wave_product_id,
			friendly_id=batch_id,
			request_body=body,
			error_message=str(exc),
			stack_trace=frappe.get_traceback(),
		)
		return exc.wave_code != WAVE_CODE_PRODUCT_NOT_FOUND

	log_step(
		correlation_id=correlation_id,
		step=STEP_PUSH_SUCCESS,
		level="Info",
		doc_type="Item",
		linked_doctype="Item",
		linked_docname=item_code,
		wave_id=wave_product_id,
		friendly_id=batch_id,
		request_body=body,
		response_body=response,
	)
	return True


def _get_or_resolve_wave_product_id(item_code: str, settings, correlation_id: str) -> str | None:
	"""Return the cached Wave product id for an Item, resolving it on first use."""
	cached = frappe.db.get_value("Item", item_code, "wave_product_id")
	if cached:
		return cached
	return product_resolver.resolve_wave_product_id(item_code, settings, correlation_id)


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
