"""Resolve and cache the Wave-side product `_id` for an ERP Item.

The stock-sync endpoint is keyed on Wave's Mongo-style product `_id`, not on
sku — so before we can push stock for an Item we have to translate
`Item.item_code` (== Wave product `sku`) into that `_id`. Wave exposes a
direct lookup at `GET /api/v3/products/by-sku/{sku}` which returns 200 with
`_id` when the product exists, and 200 with an empty body when it doesn't
(documented quirk: not a 404).

This module owns three concerns:

1. Calling that endpoint via wave_client and classifying the outcome
   (resolved / not_found / search_failed) into Wave Sync Log rows so any
   resolution attempt is auditable.
2. Persisting the resolved `_id` on `Item.wave_product_id` so subsequent
   pushes skip the lookup.
3. Refreshing the cached `_id` on demand — used by the stock pusher when
   Wave returns PRODUCT0006 ("product with id not found"), which means the
   product was deleted and recreated under a new `_id`.

It does NOT push stock and it does NOT decide whether to retry; those are
the pusher's concerns. Keeping the resolver pure makes both halves easy to
test in isolation.
"""

from __future__ import annotations

import frappe

from wave_sync_hypa.wave_sync_hypa.services import wave_client
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveOutboundError

STEP_RESOLVE_ATTEMPT = "product_resolve_attempt"
STEP_RESOLVE_SUCCESS = "product_resolve_success"
STEP_RESOLVE_NOT_FOUND = "product_resolve_not_found"
STEP_RESOLVE_SEARCH_FAILED = "product_resolve_search_failed"


def resolve_wave_product_id(
	item_code: str,
	settings,
	correlation_id: str,
	*,
	persist: bool = True,
) -> str | None:
	"""Look up an Item's Wave product `_id` and (by default) persist it on the Item.

	Returns the Wave `_id` string when Wave knows the sku, or None when the
	product is missing or the lookup itself failed. Callers that find a None
	should treat it as an operator-actionable gap (the integration cannot
	push stock until somebody resolves the catalog mismatch on Wave's side
	or fixes the item_code on the ERP side).

	Pass persist=False from contexts that don't want to write the resolved
	id back to the Item — e.g. an admin "preview" probe in the Desk.
	"""
	config = _resolve_outbound_config(settings)
	if config is None:
		log_step(
			correlation_id=correlation_id,
			step=STEP_RESOLVE_SEARCH_FAILED,
			level="Error",
			doc_type="Item",
			linked_doctype="Item",
			linked_docname=item_code,
			error_message="Wave outbound config incomplete (base_url / api_key / app_id).",
		)
		return None

	log_step(
		correlation_id=correlation_id,
		step=STEP_RESOLVE_ATTEMPT,
		level="Info",
		doc_type="Item",
		linked_doctype="Item",
		linked_docname=item_code,
		request_body={"sku": item_code},
	)

	try:
		body = wave_client.get_product_by_sku(
			base_url=config["base_url"],
			api_key=config["api_key"],
			app_id=config["app_id"],
			sku=item_code,
		)
	except WaveOutboundError as exc:
		log_step(
			correlation_id=correlation_id,
			step=STEP_RESOLVE_SEARCH_FAILED,
			level="Error",
			doc_type="Item",
			linked_doctype="Item",
			linked_docname=item_code,
			error_message=str(exc),
			stack_trace=frappe.get_traceback(),
		)
		return None

	if body is None:
		# Wave's "no product with that sku" response: 200 + empty body.
		# Surface a clear operator-actionable row rather than a generic error.
		log_step(
			correlation_id=correlation_id,
			step=STEP_RESOLVE_NOT_FOUND,
			level="Error",
			doc_type="Item",
			linked_doctype="Item",
			linked_docname=item_code,
			error_message=(
				f"Wave returned no product for sku='{item_code}'. "
				"Either the item is missing on Wave or the item_code does not match the Wave sku."
			),
		)
		# Belt-and-braces: also write to Error Log so the team's default
		# triage surface (the Frappe Error Log list) lights up.
		frappe.log_error(
			title=f"wave_sync_hypa: product not found on Wave for {item_code}",
			message=(
				f"GET /api/v3/products/by-sku/{item_code} returned 200 with empty body. "
				"This means Wave does not have a product with that sku. Action: confirm "
				"the sku in Wave's catalog matches Item.item_code on this site, or have "
				"the Wave catalog team add/restore the product."
			),
		)
		return None

	wave_id = body["_id"]
	log_step(
		correlation_id=correlation_id,
		step=STEP_RESOLVE_SUCCESS,
		level="Info",
		doc_type="Item",
		linked_doctype="Item",
		linked_docname=item_code,
		wave_id=wave_id,
		response_body={"_id": wave_id, "sku": body.get("sku"), "name": body.get("name")},
	)

	if persist:
		_persist_wave_product_id(item_code, wave_id)

	return wave_id


def _persist_wave_product_id(item_code: str, wave_id: str) -> None:
	"""Write Item.wave_product_id without re-running validate on the Item.

	The Item controller is heavy (it touches inventory, BOM rebuilds, search
	index updates) and we do not want a stock-push side effect to trigger any
	of that. set_value with update_modified=False is the documented escape
	hatch for an integration column.
	"""
	frappe.db.set_value(
		"Item", item_code, "wave_product_id", wave_id, update_modified=False
	)


def _resolve_outbound_config(settings) -> dict | None:
	"""Pull base_url, app_id, api_key from settings; None when any piece missing."""
	base_url = (settings.get("wave_api_base_url") or "").strip()
	app_id = (settings.get("wave_app_id") or "").strip()
	api_key = settings.get_password("wave_api_key", raise_exception=False) or ""
	if not (base_url and app_id and api_key):
		return None
	return {"base_url": base_url, "app_id": app_id, "api_key": api_key}
