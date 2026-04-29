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


def _build_stock_sync_url(base_url: str, product_id: str) -> str:
	"""Compose the per-product stock-sync URL, normalising trailing slashes on the base."""
	return f"{base_url.rstrip('/')}/api/v3/admin/products/{product_id}/stock/sync"


def _build_order_status_url(base_url: str, order_id: str, status_name: str) -> str:
	"""Compose the path-keyed order-status URL per Wave's spec."""
	return f"{base_url.rstrip('/')}/api/v3/admin/orders/{order_id}/status/{status_name}"


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
