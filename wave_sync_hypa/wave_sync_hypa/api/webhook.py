"""Inbound webhook endpoint for Wave.

Lives in the `api/` layer so its single responsibility — authenticate,
validate transport shape, log receipt, enqueue — is obvious. No business
logic runs here; everything heavy happens in the background worker via
`services.processor.process_webhook`.

Flow per request:
 1. Generate a correlation_id for this webhook.
 2. Load Wave Settings; refuse if disabled.
 3. Authenticate with `x-api-key` against the stored secret.
 4. Parse `?doc=` and the JSON body.
 5. Log "Received" with the snapshot.
 6. Enqueue the processor with a deterministic job_name (queue-level dedup).
 7. Log "Enqueued" and return 200 immediately.
"""

import hashlib
import hmac

import frappe

from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveAuthError, WaveValidationError


PROCESSOR_PATH = "wave_sync_hypa.wave_sync_hypa.services.processor.process_webhook"


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive():
	"""HTTP entry: ack first, process later. Returns {ok, correlation_id} with HTTP 200."""
	correlation_id = new_correlation_id()
	try:
		settings = _load_enabled_settings()
		_authenticate(settings, correlation_id)
		doc_type = _read_doc_query()
		body = _read_body()
		action = _read_action(body)
		payload = body.get("payload") or {}
	except WaveAuthError as exc:
		_abort(exc, correlation_id, http_status=403)
	except WaveValidationError as exc:
		_abort(exc, correlation_id, http_status=400)

	log_step(
		correlation_id,
		"Received",
		"Info",
		doc_type=doc_type,
		action=action,
		wave_id=payload.get("_id"),
		wave_updated_at=payload.get("updatedAt"),
		friendly_id=payload.get("friendlyId"),
		request_body=body,
	)
	_enqueue_processing(correlation_id, doc_type, action, payload)
	log_step(
		correlation_id,
		"Enqueued",
		"Info",
		doc_type=doc_type,
		action=action,
		wave_id=payload.get("_id"),
		wave_updated_at=payload.get("updatedAt"),
		friendly_id=payload.get("friendlyId"),
	)
	return {"ok": True, "correlation_id": correlation_id}


def _load_enabled_settings():
	"""Return the Wave Settings doc if the integration is enabled, else raise WaveAuthError."""
	settings = frappe.get_cached_doc("Wave Settings")
	if not settings.enabled:
		raise WaveAuthError("Wave integration is disabled")
	return settings


def _authenticate(settings, correlation_id: str) -> None:
	"""Compare the x-api-key header to the stored secret in constant time; raise WaveAuthError on mismatch.

	The audit log is committed before we raise so it survives the transaction rollback
	Frappe performs when the whitelisted method returns an exception. Without the commit
	the row would be silently dropped and operators would have no record of the rejected
	request — the exact case where the audit trail matters most.
	"""
	provided = _read_header("x-api-key")
	stored = settings.get_password("inbound_api_key", raise_exception=False) or ""
	if not stored:
		log_step(
			correlation_id,
			"Authenticated",
			"Error",
			error_message="Inbound API key not configured on Wave Settings",
		)
		frappe.db.commit()
		raise WaveAuthError("Inbound API key not configured")
	if not provided or not hmac.compare_digest(provided, stored):
		log_step(
			correlation_id,
			"Authenticated",
			"Error",
			error_message=_auth_mismatch_diagnostic(provided, stored),
			response_body=_auth_mismatch_fingerprints(provided, stored),
		)
		frappe.db.commit()
		raise WaveAuthError("Invalid API key")


def _read_header(name: str) -> str:
	"""Return a single HTTP header value (lowercase-matched) or empty string."""
	request = getattr(frappe, "request", None)
	if request is None or not getattr(request, "headers", None):
		return ""
	return request.headers.get(name) or ""


def _auth_mismatch_diagnostic(provided: str, stored: str) -> str:
	"""Return a short one-line summary of why the x-api-key comparison failed."""
	if not provided:
		return "x-api-key header missing from the request"
	if len(provided) != len(stored):
		return (
			f"x-api-key length mismatch (provided={len(provided)}, "
			f"stored={len(stored)}) — check the client is sending the full value without truncation or extra whitespace"
		)
	return (
		"x-api-key content mismatch (same length) — rotate the key and update the client, "
		"or check for stale credentials cached in the client"
	)


def _auth_mismatch_fingerprints(provided: str, stored: str) -> dict:
	"""Return secret-safe fingerprints so operators can compare server vs. client without leaking the key."""
	return {
		"provided_length": len(provided or ""),
		"stored_length": len(stored or ""),
		"provided_sha256_prefix": _sha_prefix(provided),
		"stored_sha256_prefix": _sha_prefix(stored),
		"provided_is_ascii": (provided or "").isascii(),
		"provided_first_4": (provided or "")[:4],
		"provided_last_4": (provided or "")[-4:] if provided else "",
	}


def _sha_prefix(value: str) -> str:
	"""Return the first 16 hex chars of the SHA256 digest — enough for comparison, not enough to brute force."""
	if not value:
		return ""
	return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _read_doc_query() -> str:
	"""Return the required ?doc= query parameter or raise WaveValidationError."""
	args = getattr(frappe.request, "args", None)
	doc = args.get("doc") if args else None
	if not doc:
		raise WaveValidationError("Query parameter `doc` is required")
	return doc


def _read_body() -> dict:
	"""Return the JSON request body as a dict or raise WaveValidationError."""
	body = frappe.request.get_json(silent=True) if getattr(frappe, "request", None) else None
	if not isinstance(body, dict):
		raise WaveValidationError("Request body must be a JSON object")
	return body


def _read_action(body: dict) -> str:
	"""Return the required `action` field from the body or raise WaveValidationError."""
	action = body.get("action")
	if not action:
		raise WaveValidationError("Body field `action` is required")
	return action


def _enqueue_processing(correlation_id: str, doc_type: str, action: str, payload: dict) -> None:
	"""Schedule the background processor with a deterministic job_name for queue-level dedup."""
	frappe.enqueue(
		PROCESSOR_PATH,
		queue="long",
		timeout=600,
		job_name=_job_name(doc_type, payload),
		enqueue_after_commit=True,
		correlation_id=correlation_id,
		doc_type=doc_type,
		action=action,
		payload=payload,
	)


def _job_name(doc_type: str, payload: dict) -> str:
	"""Return a deterministic RQ job name so repeated webhooks with the same updatedAt dedupe."""
	wave_id = payload.get("_id") or "unknown"
	updated_at = payload.get("updatedAt") or "initial"
	return f"wave-{doc_type}-{wave_id}-{updated_at}"


def _abort(exc: Exception, correlation_id: str, http_status: int):
	"""Log the failure, commit the audit trail, and translate to an HTTP response Frappe will return."""
	log_step(
		correlation_id,
		"Failed",
		"Error",
		error_message=str(exc)[:500],
	)
	frappe.db.commit()
	frappe.local.response.http_status_code = http_status
	frappe.local.response["ok"] = False
	frappe.local.response["error"] = str(exc)
	frappe.local.response["correlation_id"] = correlation_id
	raise frappe.PermissionError(str(exc)) if http_status == 403 else frappe.ValidationError(str(exc))
