"""HTTP surface for operator-facing Sales Order actions driven by the Wave Sync UI.

Kept separate from `api/webhook.py` so the inbound-webhook layer stays a single
concern and the operator UI endpoints have their own home.
"""

import frappe


@frappe.whitelist()
def clear_manual_review_flag(sales_order: str) -> dict:
	"""Clear Sales Order.wave_manual_review_required after operator acknowledgement."""
	doc = frappe.get_doc("Sales Order", sales_order)
	doc.check_permission("write")
	_clear_flag(sales_order)
	_record_acknowledgement(doc)
	frappe.db.commit()
	return {"ok": True, "sales_order": sales_order}


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
