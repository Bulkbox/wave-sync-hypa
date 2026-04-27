"""Whitelisted endpoint for the Sales Order form's 'Sync Order Status to Wave' button.

Lets operators replay the current Wave status mapping for one Sales Order
without waiting for a fresh submit/cancel. Reuses the same resolver +
worker as the automatic doc_event path; only the trigger is different.
"""

from __future__ import annotations

import frappe
from frappe import _

from wave_sync_hypa.wave_sync_hypa.handlers.order_status import (
	WORKER_DOTTED_PATH,
	STEP_ENQUEUED,
)
from wave_sync_hypa.wave_sync_hypa.services import order_status_resolver
from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step

DOCSTATUS_TO_EVENT = {1: "submit", 2: "cancel"}


@frappe.whitelist()
def resync_order_status(sales_order: str) -> dict:
	"""Re-derive and push the Wave status mapping for one Sales Order; return the correlation id."""
	frappe.only_for("System Manager")
	doc = frappe.get_doc("Sales Order", sales_order)
	doc.check_permission("read")
	_refuse_if_not_pushable(doc)
	settings = frappe.get_doc("Wave Settings")
	_refuse_if_settings_disabled(settings)

	event = _event_from_docstatus(doc.docstatus)
	payload = order_status_resolver.resolve_outbound_payload(doc, event, settings)
	if not payload:
		frappe.throw(
			_(
				"No enabled Outbound Status Rule matches Sales Order + {0}. "
				"Add a rule in Wave Settings before retrying."
			).format(event)
		)

	correlation_id = new_correlation_id()
	frappe.enqueue(
		WORKER_DOTTED_PATH,
		queue="default",
		sales_order_name=doc.name,
		event=event,
		payload=payload,
		correlation_id=correlation_id,
	)
	log_step(
		correlation_id=correlation_id,
		step=STEP_ENQUEUED,
		level="Info",
		doc_type="Sales Order",
		action=event,
		linked_doctype="Sales Order",
		linked_docname=doc.name,
		wave_id=doc.get("wave_order_id"),
		request_body={"event": event, "payload": payload, "triggered_by": "manual_button"},
	)
	return {"ok": True, "correlation_id": correlation_id, "event": event, "payload": payload}


def _refuse_if_not_pushable(doc) -> None:
	"""Refuse loud-and-clear when the SO has no Wave id or is still a draft."""
	if not doc.get("wave_order_id"):
		frappe.throw(_("Sales Order has no Wave Order ID; nothing to push."))
	if doc.docstatus == 0:
		frappe.throw(_("Submit or cancel the Sales Order first; nothing to push for a draft."))


def _refuse_if_settings_disabled(settings) -> None:
	"""Loud-fail at click time when the kill-switch is off rather than queueing a no-op."""
	if not settings.outbound_order_status_sync_enabled:
		frappe.throw(_("Outbound Order Status Sync is disabled in Wave Settings; turn it on first."))


def _event_from_docstatus(docstatus: int) -> str:
	"""Translate the SO's docstatus into the rule-table event name."""
	event = DOCSTATUS_TO_EVENT.get(int(docstatus))
	if not event:
		frappe.throw(_("Unsupported Sales Order docstatus {0}.").format(docstatus))
	return event
