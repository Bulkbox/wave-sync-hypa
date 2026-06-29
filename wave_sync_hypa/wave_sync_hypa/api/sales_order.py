"""HTTP surface for operator-facing Sales Order actions driven by the Wave Sync UI.

Kept separate from `api/webhook.py` so the inbound-webhook layer stays a single
concern and the operator UI endpoints have their own home.
"""

import frappe
from frappe import _

from wave_sync_hypa.wave_sync_hypa.handlers import prepaid_pe
from wave_sync_hypa.wave_sync_hypa.services import ipay_payment_sync, wave_order_creator
from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id


@frappe.whitelist()
def clear_manual_review_flag(sales_order: str) -> dict:
	"""Clear Sales Order.wave_manual_review_required after operator acknowledgement."""
	doc = frappe.get_doc("Sales Order", sales_order)
	doc.check_permission("write")
	_clear_flag(sales_order)
	_record_acknowledgement(doc)
	frappe.db.commit()
	return {"ok": True, "sales_order": sales_order}


@frappe.whitelist()
def push_to_wave(sales_order: str) -> dict:
	"""Operator-triggered ERP -> Wave order push for offline Sales Orders.

	Invoked by the 'Push to Wave' button on the Sales Order form. Validates
	write permission, generates a correlation id, delegates the heavy lifting
	to wave_order_creator.push_so_to_wave (which never raises), then forwards
	its structured result to the client.

	Returns:
	  {"ok": True, "wave_order_id": "...", "wave_friendly_id": "...", "correlation_id": "..."}
	  {"ok": False, "reason": "<message>", "correlation_id": "..."}
	"""
	doc = frappe.get_doc("Sales Order", sales_order)
	doc.check_permission("write")
	correlation_id = new_correlation_id()
	result = wave_order_creator.push_so_to_wave(sales_order, correlation_id)
	result["correlation_id"] = correlation_id
	frappe.db.commit()
	return result


@frappe.whitelist()
def verify_ipay_payment(sales_order: str) -> dict:
	"""Operator-triggered iPay payment verification for a prepaid Sales Order.

	Invoked by the 'Verify iPay Payment' button. Synchronously looks the
	payment up on iPay by the order's Wave friendly id (the iPay oid), stamps
	the wave_ipay_* fields, sets/clears the accounting review flag, and returns
	the details for the button to render. Resilient to iPay being absent,
	unconfigured, or unreachable — the gateway degrades to "not verified"
	rather than raising. (Honours the master switch + ipay_verification_enabled
	via fetch_and_stamp.)

	Every return carries the same shape so the JS can render uniformly:
	  {"ok": True,  "paid": True,  "data": {...}, "reason": None, "correlation_id": "..."}
	  {"ok": True,  "paid": False, "data": None,  "reason": "...", "correlation_id": "..."}
	  {"ok": False, "paid": False, "data": None,  "reason": "...", "correlation_id": "..."}
	"""
	correlation_id = new_correlation_id()
	doc = frappe.get_doc("Sales Order", sales_order)
	doc.check_permission("read")
	if (doc.get("wave_payment_classification") or "") != "prepaid":
		return {
			"ok": False, "paid": False, "data": None,
			"reason": _("This is not a prepaid Wave order."),
			"correlation_id": correlation_id,
		}
	result = ipay_payment_sync.fetch_and_stamp(sales_order, correlation_id)
	# A successful verify on a confirmed (submitted) prepaid order is the trigger
	# to create the unallocated draft Payment Entry (gated; the worker no-ops if
	# the feature is off or the order is still a draft).
	if result.get("paid") and doc.docstatus == 1:
		prepaid_pe.maybe_enqueue_draft_for_order(sales_order)
	result["correlation_id"] = correlation_id
	frappe.db.commit()
	return result


def _clear_flag(sales_order: str) -> None:
	"""Persist wave_manual_review_required=0 via direct DB write (skips re-running validate)."""
	frappe.db.set_value(
		"Sales Order",
		sales_order,
		"wave_manual_review_required",
		0,
		update_modified=False,
	)


def _record_acknowledgement(doc) -> None:
	"""Append a timeline Comment naming the user who cleared the flag."""
	doc.add_comment(
		"Comment",
		f"Wave Sync: manual-review flag cleared by <b>{frappe.session.user}</b>.",
	)
