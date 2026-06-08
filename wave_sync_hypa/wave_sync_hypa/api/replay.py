"""Operator-initiated replay of a failed Wave webhook.

The `Received` Wave Sync Log row for a correlation preserves the original
payload, so re-processing needs no new storage. Replay never auto-runs — an
operator triggers it from the Wave Sync Log form after fixing the root cause.
The updated_at idempotency check is bypassed (force=True); the handler's own
existing-record lookup still prevents creating a duplicate Sales Order.
"""

from __future__ import annotations

import json

import frappe
from frappe import _

from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id
from wave_sync_hypa.wave_sync_hypa.services.processor import process_webhook


@frappe.whitelist()
def replay_order(correlation_id: str) -> dict:
	"""Re-process the webhook captured under `correlation_id`, bypassing updated_at dedup."""
	if not frappe.has_permission("Sales Order", "create"):
		frappe.throw(_("You need permission to create Sales Orders to replay an order."), frappe.PermissionError)

	row = frappe.db.get_value(
		"Wave Sync Log",
		{"correlation_id": correlation_id, "step": "Received"},
		["doc_type", "action", "request_body"],
		as_dict=True,
		order_by="creation asc",
	)
	if not row:
		frappe.throw(_("No received webhook is stored for correlation {0}.").format(correlation_id))

	payload = _payload_from(row.request_body)
	if not payload:
		frappe.throw(_("The stored webhook for correlation {0} has no replayable payload.").format(correlation_id))

	new_correlation = new_correlation_id()
	process_webhook(new_correlation, row.doc_type, row.action, payload, force=True)
	return {"ok": True, "correlation_id": new_correlation}


def _payload_from(request_body) -> dict:
	"""Extract the order payload from a stored `Received` request_body (JSON envelope)."""
	if isinstance(request_body, str):
		try:
			request_body = json.loads(request_body)
		except (ValueError, TypeError):
			return {}
	if not isinstance(request_body, dict):
		return {}
	payload = request_body.get("payload")
	return payload if isinstance(payload, dict) else {}
