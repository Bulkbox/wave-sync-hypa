"""Orchestrate the ERP -> Wave order push end to end.

Single public entry point `push_so_to_wave(so_name, correlation_id)`. Calls
the customer resolver, the order builder, then `wave_client.create_admin_order`.
On success, stamps `wave_order_id`, `wave_friendly_id`, `wave_origin="ERP Push"`
on the Sales Order and clears any prior failure flag. On any failure, sets
`wave_push_failure_required_review=1` (drives the form banner), appends a
Comment to the SO timeline, and writes an Error row to Wave Sync Log.

This function never raises — callers (whitelisted API, future operator
button) get back a structured `{ok, reason?, wave_order_id?, wave_friendly_id?}`
dict and drive their UI from it.
"""

from __future__ import annotations

import frappe

from wave_sync_hypa.wave_sync_hypa.services import (
	intake_review_notifier,
	wave_client,
	wave_customer_resolver,
	wave_order_builder,
)
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step
from wave_sync_hypa.wave_sync_hypa.services.master_switch import (
	STEP_MASTER_DISABLED,
	is_wave_integration_enabled,
)
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveOutboundError, WaveResolutionError

STEP_PUSH_ATTEMPT = "erp_to_wave_push_attempt"
STEP_PUSH_SUCCEEDED = "erp_to_wave_push_succeeded"
STEP_PUSH_FAILED = "erp_to_wave_push_failed"
STEP_PUSH_ABORTED_DISABLED = "erp_to_wave_push_aborted_disabled"
STEP_PUSH_ABORTED_ALREADY_PUSHED = "erp_to_wave_push_aborted_already_pushed"
STEP_PUSH_ABORTED_MISSING_CONFIG = "erp_to_wave_push_aborted_missing_config"
STEP_PUSH_ABORTED_MISSING_SHOP = "erp_to_wave_push_aborted_missing_shop_id"
STEP_PUSH_ABORTED_UNRESOLVABLE = "erp_to_wave_push_aborted_unresolvable_items"


def push_so_to_wave(so_name: str, correlation_id: str) -> dict:
	"""Push a Sales Order to Wave; return a structured result dict (never raises)."""
	if not is_wave_integration_enabled():
		log_step(
			correlation_id, STEP_MASTER_DISABLED, "Info",
			doc_type="Sales Order", linked_doctype="Sales Order", linked_docname=so_name,
			error_message="Wave integration master kill switch is off.",
		)
		return {"ok": False, "reason": "Wave integration is disabled in Wave Settings"}

	settings = frappe.get_cached_doc("Wave Settings")

	if not settings.get("erp_to_wave_push_enabled"):
		return _abort_silently(
			so_name, correlation_id, STEP_PUSH_ABORTED_DISABLED,
			"ERP → Wave Order Push is disabled in Wave Settings.",
		)

	config = _resolve_outbound_config(settings)
	if config is None:
		return _abort_with_notification(
			so_name, settings, correlation_id, STEP_PUSH_ABORTED_MISSING_CONFIG,
			"Wave outbound config incomplete (base_url / api_key / app_id).",
		)

	shop_id = (settings.get("wave_shop_id") or "").strip()
	if not shop_id:
		return _abort_with_notification(
			so_name, settings, correlation_id, STEP_PUSH_ABORTED_MISSING_SHOP,
			"Wave Settings → ERP → Wave Order Push → Wave Shop ID is not configured.",
		)

	so = frappe.get_doc("Sales Order", so_name)
	if so.get("wave_order_id"):
		return _abort_silently(
			so_name, correlation_id, STEP_PUSH_ABORTED_ALREADY_PUSHED,
			f"Sales Order is already linked to Wave order {so.wave_order_id}.",
		)

	log_step(
		correlation_id, STEP_PUSH_ATTEMPT, "Info",
		doc_type="Sales Order", linked_doctype="Sales Order", linked_docname=so_name,
	)

	try:
		customer_id = wave_customer_resolver.resolve_wave_customer_for_so(so, settings)
	except WaveResolutionError as exc:
		return _abort_with_notification(so_name, settings, correlation_id, STEP_PUSH_FAILED, str(exc))

	try:
		body = wave_order_builder.build_order_payload(so, customer_id, settings, correlation_id, config)
	except WaveResolutionError as exc:
		return _abort_with_notification(so_name, settings, correlation_id, STEP_PUSH_ABORTED_UNRESOLVABLE, str(exc))
	except WaveOutboundError as exc:
		return _abort_with_notification(so_name, settings, correlation_id, STEP_PUSH_FAILED, f"Catalog GET failed: {exc}")

	try:
		response = wave_client.create_admin_order(
			base_url=config["base_url"],
			api_key=config["api_key"],
			app_id=config["app_id"],
			body=body,
			skip_webhook_notification=True,
		)
	except WaveOutboundError as exc:
		return _abort_with_notification(so_name, settings, correlation_id, STEP_PUSH_FAILED, f"Wave POST failed: {exc}")

	wave_order_id = (response.get("_id") or "").strip()
	wave_friendly_id = (response.get("friendlyId") or "").strip()
	if not wave_order_id:
		return _abort_with_notification(
			so_name, settings, correlation_id, STEP_PUSH_FAILED,
			"Wave POST returned 201 but no _id in the response body.",
		)

	_stamp_success(so_name, wave_order_id, wave_friendly_id, correlation_id, body, response)
	return {"ok": True, "wave_order_id": wave_order_id, "wave_friendly_id": wave_friendly_id}


def _stamp_success(
	so_name: str,
	wave_order_id: str,
	wave_friendly_id: str,
	correlation_id: str,
	body: dict,
	response: dict,
) -> None:
	"""Mutating success path: persist Wave linkage, clear failure flag, log."""
	so = frappe.get_doc("Sales Order", so_name)
	so.db_set("wave_order_id", wave_order_id, update_modified=False)
	so.db_set("wave_friendly_id", wave_friendly_id or "", update_modified=False)
	so.db_set("wave_origin", "ERP Push", update_modified=False)
	so.db_set("wave_push_failure_required_review", 0, update_modified=False)
	# Stamp the Wave friendly id into Customer's Purchase Order (po_no) so it is
	# visible/searchable in the standard SO list view's PO column. Only when
	# empty — the operator may have entered a paper PO manually before the push,
	# and overwriting it would lose information.
	if wave_friendly_id and not (so.po_no or "").strip():
		so.db_set("po_no", wave_friendly_id, update_modified=False)
	so.add_comment(
		"Comment",
		f"Pushed to Wave successfully — wave_order_id = <code>{wave_order_id}</code>, "
		f"friendlyId = <code>{wave_friendly_id or '—'}</code>.",
	)
	log_step(
		correlation_id, STEP_PUSH_SUCCEEDED, "Success",
		doc_type="Sales Order", linked_doctype="Sales Order", linked_docname=so_name,
		wave_id=wave_order_id, friendly_id=wave_friendly_id or None,
		request_body=body,
		response_body={"_id": wave_order_id, "friendlyId": wave_friendly_id, "status": response.get("status")},
	)


def _abort_with_notification(so_name: str, settings, correlation_id: str, step: str, reason: str) -> dict:
	"""Notify-the-operator failure path: banner + Comment + ToDo + Error log row."""
	so = frappe.get_doc("Sales Order", so_name)
	so.db_set("wave_push_failure_required_review", 1, update_modified=False)
	so.add_comment("Comment", f"<b>Wave push failed:</b> {reason}")
	log_step(
		correlation_id, step, "Error",
		doc_type="Sales Order", linked_doctype="Sales Order", linked_docname=so_name,
		error_message=reason,
	)
	# Best-effort ToDo creation — gated by wave_push_failure_todo_enabled in Wave
	# Settings. Failures here don't propagate; the banner + Comment + log row are
	# the canonical notification surfaces.
	try:
		intake_review_notifier.notify_sales_order_push_failed(so_name, settings, reason)
	except Exception:
		pass
	return {"ok": False, "reason": reason}


def _abort_silently(so_name: str, correlation_id: str, step: str, reason: str) -> dict:
	"""Pre-condition failure path: Info log only, no banner / Comment (not a runtime error)."""
	log_step(
		correlation_id, step, "Info",
		doc_type="Sales Order", linked_doctype="Sales Order", linked_docname=so_name,
		error_message=reason,
	)
	return {"ok": False, "reason": reason}


def _resolve_outbound_config(settings) -> dict | None:
	"""Pull base_url / api_key / app_id; return None if any required piece is missing."""
	base_url = (settings.get("wave_api_base_url") or "").strip()
	app_id = (settings.get("wave_app_id") or "").strip()
	api_key = settings.get_password("wave_api_key", raise_exception=False) or ""
	if not (base_url and app_id and api_key):
		return None
	return {"base_url": base_url, "app_id": app_id, "api_key": api_key}
