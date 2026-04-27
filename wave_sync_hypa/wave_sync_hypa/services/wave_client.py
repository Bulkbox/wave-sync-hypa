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

	if not (200 <= response.status_code < 300):
		raise WaveOutboundError(
			f"Wave stock/sync returned HTTP {response.status_code}: {_safe_text(response)}"
		)

	return _parse_json(response)


def put_order_update(
	*,
	base_url: str,
	api_key: str,
	app_id: str,
	order_id: str,
	body: dict,
	skip_webhook_notification: bool = True,
	timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
	"""PUT a partial order update to Wave; skipWebhookNotification flag controlled by caller."""
	if not base_url:
		raise WaveOutboundError("Wave API base URL is not configured.")
	if not api_key:
		raise WaveOutboundError("Wave API key is not configured.")
	if not app_id:
		raise WaveOutboundError("Wave App ID is not configured.")
	if not order_id:
		raise WaveOutboundError("order_id is required.")
	if not body:
		raise WaveOutboundError("body must contain at least one field to update.")

	url = _build_order_update_url(base_url, order_id, skip_webhook_notification)
	headers = _build_headers(api_key, app_id)

	try:
		response = requests.put(url, json=body, headers=headers, timeout=timeout)
	except requests.RequestException as exc:
		raise WaveOutboundError(f"network error calling Wave order update: {exc}") from exc

	if not (200 <= response.status_code < 300):
		raise WaveOutboundError(
			f"Wave order update returned HTTP {response.status_code}: {_safe_text(response)}"
		)

	return _parse_json(response)


def _build_stock_sync_url(base_url: str, product_id: str) -> str:
	"""Compose the per-product stock-sync URL, normalising trailing slashes on the base."""
	return f"{base_url.rstrip('/')}/api/v3/admin/products/{product_id}/stock/sync"


def _build_order_update_url(base_url: str, order_id: str, skip_webhook_notification: bool) -> str:
	"""Compose the per-order update URL; query string carries the skip flag verbatim per Wave's spec."""
	flag = "true" if skip_webhook_notification else "false"
	return (
		f"{base_url.rstrip('/')}/api/v3/admin/orders/{order_id}"
		f"?skipWebhookNotification={flag}"
	)


def _build_headers(api_key: str, app_id: str) -> dict:
	"""Assemble the standard Wave request headers."""
	return {
		"X-API-Key": api_key,
		"appId": app_id,
		"accept": "application/json",
		"content-type": "application/json",
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
