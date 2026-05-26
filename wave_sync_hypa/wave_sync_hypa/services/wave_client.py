"""Thin HTTP client for outbound calls to the Wave REST API.

Single concern: build the request, send it, raise on non-2xx. No retries, no
logging, no business decisions. Callers (stock_pusher, future order-status
pushers) wrap this in their own logging + error handling so the client stays
testable and stateless.
"""

from __future__ import annotations

import requests

from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveOutboundError

DEFAULT_TIMEOUT_SECONDS = 10

# Wave exposes dedicated routes for some terminal-ish transitions (e.g. accept,
# reject, cancel) instead of the generic /status/{name} endpoint. Map those
# status names to their override paths here. Anything not listed falls through
# to the default `/api/v3/admin/orders/{order_id}/status/{status_name}` shape.
#
# Only ACCEPTED is overridden today: per Wave's spec, accepting an order is
# `POST /api/v3/admin/orders/{id}/accept` (no body, no query). Other dedicated
# routes can be added the moment Wave confirms they exist; until then we keep
# the generic path-keyed POST as the default so we don't 404 against routes
# that haven't been published.
STATUS_PATH_OVERRIDES = {
	"ACCEPTED": "/api/v3/admin/orders/{order_id}/accept",
}


def _raise_for_response(response: requests.Response, what: str) -> None:
	"""Convert a non-2xx Wave response into a structured WaveOutboundError.

	Wave's REST API consistently returns errors in the shape
	`{"code": "PRODUCT0006", "userTitle": "...", "userMessage": "...", ...}`.
	Parse that envelope when possible and attach the `code` to the exception
	so callers can branch on it (e.g. retry on PRODUCT0006, soft-skip on
	ORDER0049). Falls back to None when the body isn't JSON.
	"""
	if 200 <= response.status_code < 300:
		return
	body = _safe_text(response)
	wave_code = _extract_wave_code(response)
	raise WaveOutboundError(
		f"Wave {what} returned HTTP {response.status_code}: {body}",
		http_status=response.status_code,
		wave_code=wave_code,
		response_text=body,
	)


def _extract_wave_code(response: requests.Response) -> str | None:
	"""Best-effort: pull the `code` field out of Wave's JSON error envelope."""
	try:
		payload = response.json()
	except ValueError:
		return None
	if isinstance(payload, dict):
		code = payload.get("code")
		if isinstance(code, str) and code:
			return code
	return None


def post_stock_sync(
	*,
	base_url: str,
	api_key: str,
	app_id: str,
	product_id: str,
	store_id: str,
	quantity: int,
	timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
	"""POST an absolute stock quantity for one product to Wave; return the parsed response."""
	if not base_url:
		raise WaveOutboundError("Wave API base URL is not configured.")
	if not api_key:
		raise WaveOutboundError("Wave API key is not configured.")
	if not app_id:
		raise WaveOutboundError("Wave App ID is not configured.")
	if not store_id:
		raise WaveOutboundError("Wave Store ID is not configured.")
	if not product_id:
		raise WaveOutboundError("product_id is required.")

	url = _build_stock_sync_url(base_url, product_id)
	headers = _build_headers(api_key, app_id)
	body = {"productId": product_id, "storeId": store_id, "quantity": quantity}

	try:
		response = requests.post(url, json=body, headers=headers, timeout=timeout)
	except requests.RequestException as exc:
		raise WaveOutboundError(f"network error calling Wave stock/sync: {exc}") from exc

	_raise_for_response(response, "stock/sync")
	return _parse_json(response)


def post_order_status(
	*,
	base_url: str,
	api_key: str,
	app_id: str,
	order_id: str,
	status_name: str,
	timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
	"""POST a status transition for one order to Wave; status name lives in the URL path, no body.

	Per Wave's spec:
	    POST /api/v3/admin/orders/{order_id}/status/{status_name}
	    Headers: X-API-Key, appId
	    No body, no query string.

	The endpoint is path-keyed (one URL per status), so callers fire one HTTP
	call per status string they want to set. There is no batch / merged-body
	form on Wave's side — that's why the resolver still emits a payload but
	the worker translates each field into its own POST.
	"""
	if not base_url:
		raise WaveOutboundError("Wave API base URL is not configured.")
	if not api_key:
		raise WaveOutboundError("Wave API key is not configured.")
	if not app_id:
		raise WaveOutboundError("Wave App ID is not configured.")
	if not order_id:
		raise WaveOutboundError("order_id is required.")
	if not status_name:
		raise WaveOutboundError("status_name is required.")

	url = _build_order_status_url(base_url, order_id, status_name)
	headers = _build_status_headers(api_key, app_id)

	try:
		response = requests.post(url, headers=headers, timeout=timeout)
	except requests.RequestException as exc:
		raise WaveOutboundError(f"network error calling Wave order status: {exc}") from exc

	_raise_for_response(response, "order status")
	return _parse_json(response)


def reject_admin_order(
	*,
	base_url: str,
	api_key: str,
	app_id: str,
	order_id: str,
	timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
	"""POST /api/v3.1/admin/orders/{order_id}/reject.

	The cancel path for both ERP-pushed AND Wave-originated orders. Authorized
	for admin tokens; refuses prepaid orders with ORDER0005 (matches Wave UI).
	Returns the full order body on 200 (cancelType="MERCHANT").
	"""
	if not base_url:
		raise WaveOutboundError("Wave API base URL is not configured.")
	if not api_key:
		raise WaveOutboundError("Wave API key is not configured.")
	if not app_id:
		raise WaveOutboundError("Wave App ID is not configured.")
	if not order_id:
		raise WaveOutboundError("order_id is required.")

	url = f"{base_url.rstrip('/')}/api/v3.1/admin/orders/{order_id}/reject"
	headers = _build_status_headers(api_key, app_id)
	try:
		response = requests.post(url, headers=headers, timeout=timeout)
	except requests.RequestException as exc:
		raise WaveOutboundError(f"network error calling Wave admin reject: {exc}") from exc

	_raise_for_response(response, "admin reject")
	return _parse_json(response)


def patch_order_products(
	*,
	base_url: str,
	api_key: str,
	app_id: str,
	order_id: str,
	body: list,
	skip_webhook_notification: bool = False,
	recalculated_derived_fields: bool = False,
	timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
	"""PATCH /api/v3/admin/orders/{order_id}/products with a raw array of product partials.

	Wave exposes two distinct PATCH routes on an order:

	  * /admin/orders/{id}            -- partial OrderV3, order-level scalars
	    (status, comments, etc.). Silently ignores `products` entries.
	  * /admin/orders/{id}/products   -- raw array body, mutates line items.

	This helper is for the second one. Body is a list of OrderProductV3 partials,
	NOT wrapped in `{"products": [...]}`. Callers are responsible for shaping
	each entry minimally — Wave only updates the keys that are present.

	`skip_webhook_notification` -> ?skipWebhookNotification=
	`recalculated_derived_fields` -> ?recalculatedDerivedFields=
	    Default False because batch-id-only PATCHes must not disturb pricing
	    or other computed order fields. Wave's server-side default is True;
	    we deliberately invert that for safety.
	"""
	if not base_url:
		raise WaveOutboundError("Wave API base URL is not configured.")
	if not api_key:
		raise WaveOutboundError("Wave API key is not configured.")
	if not app_id:
		raise WaveOutboundError("Wave App ID is not configured.")
	if not order_id:
		raise WaveOutboundError("order_id is required.")
	if not isinstance(body, list) or not body:
		raise WaveOutboundError("PATCH products body must be a non-empty list.")

	url = _build_admin_order_products_url(base_url, order_id)
	headers = _build_headers(api_key, app_id)
	params = {
		"skipWebhookNotification": "true" if skip_webhook_notification else "false",
		"recalculatedDerivedFields": "true" if recalculated_derived_fields else "false",
	}

	try:
		response = requests.patch(url, json=body, params=params, headers=headers, timeout=timeout)
	except requests.RequestException as exc:
		raise WaveOutboundError(f"network error calling Wave admin order products PATCH: {exc}") from exc

	_raise_for_response(response, "admin order products PATCH")
	return _parse_json(response)


def get_product_by_sku(
	*,
	base_url: str,
	api_key: str,
	app_id: str,
	sku: str,
	timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict | None:
	"""GET a Wave product by sku; return its parsed body or None when Wave reports not-found.

	Wave's contract for this endpoint is unusual: an unknown sku returns
	HTTP 200 with an empty body (Content-Length: 0), not a 404. Callers
	that need to distinguish 'product exists' from 'product missing'
	therefore cannot rely on status code alone, so this helper centralises
	the contract:

	  - 2xx with parseable body containing `_id` -> return the dict
	  - 2xx with empty body OR no `_id`          -> return None
	  - any other status                         -> raise WaveOutboundError

	The resolver layer maps None to a `product_resolve_not_found` audit
	row (operator alert) without having to inspect HTTP status itself.
	"""
	if not base_url:
		raise WaveOutboundError("Wave API base URL is not configured.")
	if not api_key:
		raise WaveOutboundError("Wave API key is not configured.")
	if not app_id:
		raise WaveOutboundError("Wave App ID is not configured.")
	if not sku:
		raise WaveOutboundError("sku is required.")

	url = _build_product_by_sku_url(base_url, sku)
	headers = _build_status_headers(api_key, app_id)

	try:
		response = requests.get(url, headers=headers, timeout=timeout)
	except requests.RequestException as exc:
		raise WaveOutboundError(f"network error calling Wave product by-sku: {exc}") from exc

	_raise_for_response(response, "product by-sku")
	if not response.content:
		return None
	body = _parse_json(response)
	if not isinstance(body, dict) or not body.get("_id"):
		return None
	return body


def patch_product(
	*,
	base_url: str,
	api_key: str,
	app_id: str,
	product_id: str,
	body: dict,
	timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
	"""PATCH /api/v3/admin/products/{id} with a partial body; return the parsed response.

	Wave's PATCH endpoint accepts any subset of the product fields and leaves
	the rest unchanged. The caller is responsible for the body's shape — this
	wrapper is intentionally generic so future partial-update needs (price,
	availability, quantityLimit, etc.) reuse one HTTP path.
	"""
	if not base_url:
		raise WaveOutboundError("Wave API base URL is not configured.")
	if not api_key:
		raise WaveOutboundError("Wave API key is not configured.")
	if not app_id:
		raise WaveOutboundError("Wave App ID is not configured.")
	if not product_id:
		raise WaveOutboundError("product_id is required.")

	url = _build_admin_product_url(base_url, product_id)
	headers = _build_headers(api_key, app_id)

	try:
		response = requests.patch(url, json=body, headers=headers, timeout=timeout)
	except requests.RequestException as exc:
		raise WaveOutboundError(f"network error calling Wave product PATCH: {exc}") from exc

	_raise_for_response(response, "admin/products PATCH")
	return _parse_json(response)


def _build_admin_product_url(base_url: str, product_id: str) -> str:
	"""Compose the admin product detail URL used by PATCH / GET /admin/products/{id}."""
	return f"{base_url.rstrip('/')}/api/v3/admin/products/{product_id}"


def _build_admin_orders_url(base_url: str) -> str:
	"""Compose the admin orders collection URL used by POST /api/v3/admin/orders."""
	return f"{base_url.rstrip('/')}/api/v3/admin/orders"


def get_admin_product_by_id(
	*,
	base_url: str,
	api_key: str,
	app_id: str,
	product_id: str,
	timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict | None:
	"""GET /api/v3/admin/products/{id}; return the full product dict, or None on 404.

	Used by the ERP -> Wave order push to backfill OrderProductV3 fields
	(name, vat, isWeighed, uom, unitOfMeasurement, categories, etc.) from
	Wave's authoritative catalog. Returning None on 404 lets callers
	distinguish "Wave deleted this product since we cached it" (stale
	wave_product_id) from real HTTP failures.
	"""
	if not base_url:
		raise WaveOutboundError("Wave API base URL is not configured.")
	if not api_key:
		raise WaveOutboundError("Wave API key is not configured.")
	if not app_id:
		raise WaveOutboundError("Wave App ID is not configured.")
	if not product_id:
		raise WaveOutboundError("product_id is required.")

	url = _build_admin_product_url(base_url, product_id)
	headers = _build_headers(api_key, app_id)

	try:
		response = requests.get(url, headers=headers, timeout=timeout)
	except requests.RequestException as exc:
		raise WaveOutboundError(f"network error calling Wave admin/products GET: {exc}") from exc

	if response.status_code == 404:
		return None
	_raise_for_response(response, "admin/products GET")
	return _parse_json(response)


def create_admin_order(
	*,
	base_url: str,
	api_key: str,
	app_id: str,
	body: dict,
	skip_webhook_notification: bool = True,
	timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
	"""POST /api/v3/admin/orders to create a new Wave-side order from the integrator.

	`skip_webhook_notification` defaults to True so the ERP-pushed order
	doesn't fire an ORDER.CREATE webhook back at us — the inbound handler
	would dedup by wave_order_id anyway, but suppressing the round-trip
	keeps the audit trail clean. Caller stamps the response's _id +
	friendlyId on the source SO.
	"""
	if not base_url:
		raise WaveOutboundError("Wave API base URL is not configured.")
	if not api_key:
		raise WaveOutboundError("Wave API key is not configured.")
	if not app_id:
		raise WaveOutboundError("Wave App ID is not configured.")
	if not isinstance(body, dict) or not body:
		raise WaveOutboundError("admin/orders POST body must be a non-empty dict.")

	url = _build_admin_orders_url(base_url)
	headers = _build_headers(api_key, app_id)
	params = {"skipWebhookNotification": "true" if skip_webhook_notification else "false"}

	try:
		response = requests.post(url, json=body, params=params, headers=headers, timeout=timeout)
	except requests.RequestException as exc:
		raise WaveOutboundError(f"network error calling Wave admin/orders POST: {exc}") from exc

	_raise_for_response(response, "admin/orders POST")
	return _parse_json(response)


def _build_stock_sync_url(base_url: str, product_id: str) -> str:
	"""Compose the per-product stock-sync URL, normalising trailing slashes on the base."""
	return f"{base_url.rstrip('/')}/api/v3/admin/products/{product_id}/stock/sync"


def _build_order_status_url(base_url: str, order_id: str, status_name: str) -> str:
	"""Compose the order-status URL, honouring per-status overrides where Wave defines them.

	Wave provides dedicated routes for a handful of transitions (currently
	just `/accept`); STATUS_PATH_OVERRIDES maps the status name to the path
	template. Everything else falls back to the generic
	`/api/v3/admin/orders/{order_id}/status/{status_name}` route.
	"""
	override = STATUS_PATH_OVERRIDES.get(status_name)
	if override:
		return f"{base_url.rstrip('/')}{override.format(order_id=order_id)}"
	return f"{base_url.rstrip('/')}/api/v3/admin/orders/{order_id}/status/{status_name}"


def _build_admin_order_products_url(base_url: str, order_id: str) -> str:
	"""Compose the admin-order products URL used by PATCH /api/v3/admin/orders/{id}/products."""
	return f"{base_url.rstrip('/')}/api/v3/admin/orders/{order_id}/products"


def _build_product_by_sku_url(base_url: str, sku: str) -> str:
	"""Compose the by-sku product lookup URL per Wave's spec."""
	return f"{base_url.rstrip('/')}/api/v3/products/by-sku/{sku}"


def _build_headers(api_key: str, app_id: str) -> dict:
	"""Assemble request headers for endpoints that send a JSON body."""
	return {
		"X-API-Key": api_key,
		"appId": app_id,
		"accept": "application/json",
		"content-type": "application/json",
	}


def _build_status_headers(api_key: str, app_id: str) -> dict:
	"""Headers for the status endpoint — no body, so omit content-type."""
	return {
		"X-API-Key": api_key,
		"appId": app_id,
		"accept": "application/json",
	}


def _safe_text(response: requests.Response) -> str:
	"""Return response body text, capped, for inclusion in error messages and logs."""
	try:
		return (response.text or "")[:500]
	except Exception:
		return "<unreadable response body>"


def _parse_json(response: requests.Response) -> dict:
	"""Best-effort JSON parse; an empty / non-JSON 2xx body is fine."""
	try:
		return response.json() if response.content else {}
	except ValueError:
		return {"raw": _safe_text(response)}
