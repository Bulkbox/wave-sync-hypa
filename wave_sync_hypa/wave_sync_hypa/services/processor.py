"""Background-worker entry point for Wave webhooks.

The HTTP endpoint enqueues this function via frappe.enqueue. The function
runs outside the request thread so handler work can take as long as it
needs without blocking Wave's retry behaviour.

One function per responsibility:
 - process_webhook  : orchestration (the public entry)
 - _run_handler     : wraps handler invocation in try/except and logs the outcome
"""

import traceback

import frappe

from wave_sync_hypa.wave_sync_hypa.services.dispatcher import resolve_handler
from wave_sync_hypa.wave_sync_hypa.services.idempotency import is_duplicate
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step


def process_webhook(
	correlation_id: str,
	doc_type: str,
	action: str,
	payload: dict,
) -> None:
	"""Handle one webhook: dedup, dispatch, log; never raise out of the worker."""
	wave_id = (payload or {}).get("_id")
	wave_updated_at = (payload or {}).get("updatedAt")
	friendly_id = (payload or {}).get("friendlyId")

	if is_duplicate(wave_id, wave_updated_at):
		log_step(
			correlation_id,
			"Skipped",
			"Info",
			doc_type=doc_type,
			action=action,
			wave_id=wave_id,
			wave_updated_at=wave_updated_at,
			friendly_id=friendly_id,
			response_body={"reason": "duplicate_updated_at"},
		)
		return

	handler = resolve_handler(doc_type, action)
	if handler is None:
		log_step(
			correlation_id,
			"Skipped",
			"Warning",
			doc_type=doc_type,
			action=action,
			wave_id=wave_id,
			wave_updated_at=wave_updated_at,
			friendly_id=friendly_id,
			response_body={"reason": "no_enabled_route_rule_or_handler_not_registered"},
		)
		return

	log_step(
		correlation_id,
		"Processing",
		"Info",
		doc_type=doc_type,
		action=action,
		wave_id=wave_id,
		wave_updated_at=wave_updated_at,
		friendly_id=friendly_id,
	)

	_run_handler(
		handler=handler,
		payload=payload,
		correlation_id=correlation_id,
		doc_type=doc_type,
		action=action,
		wave_id=wave_id,
		wave_updated_at=wave_updated_at,
		friendly_id=friendly_id,
	)


def _run_handler(
	handler,
	payload: dict,
	correlation_id: str,
	doc_type: str,
	action: str,
	wave_id: str | None,
	wave_updated_at: str | None,
	friendly_id: str | None,
) -> None:
	"""Call the handler and log Completed on success or Failed on any exception."""
	try:
		handler(payload, correlation_id)
	except Exception as exc:
		log_step(
			correlation_id,
			"Failed",
			"Error",
			doc_type=doc_type,
			action=action,
			wave_id=wave_id,
			wave_updated_at=wave_updated_at,
			friendly_id=friendly_id,
			error_message=str(exc)[:500],
			stack_trace=traceback.format_exc(),
		)
		frappe.log_error(
			title="wave_sync_hypa: handler raised",
			message=traceback.format_exc(),
		)
		return

	log_step(
		correlation_id,
		"Completed",
		"Success",
		doc_type=doc_type,
		action=action,
		wave_id=wave_id,
		wave_updated_at=wave_updated_at,
		friendly_id=friendly_id,
	)
