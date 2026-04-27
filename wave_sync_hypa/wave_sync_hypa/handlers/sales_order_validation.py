"""Sales Order validation hooks for the Wave integration.

Single-purpose: enforce conditional uniqueness on `wave_order_id`. The
Custom Field used to carry `unique=1`, which made amends impossible — a
cancelled SO holding a wave_order_id blocked the amended draft from
inheriting the same id. We now allow shared ids when the conflicting SO
is cancelled (docstatus=2), and only reject when two non-cancelled SOs
would share the id.
"""

from __future__ import annotations

import frappe
from frappe import _

DOCSTATUS_CANCELLED = 2


def validate_unique_wave_order_id(doc, method=None) -> None:
	"""Reject saves where another non-cancelled Sales Order already carries the same wave_order_id."""
	wave_order_id = (doc.get("wave_order_id") or "").strip()
	if not wave_order_id:
		return

	conflict = frappe.db.get_value(
		"Sales Order",
		filters={
			"wave_order_id": wave_order_id,
			"name": ["!=", doc.name],
			"docstatus": ["<", DOCSTATUS_CANCELLED],
		},
		fieldname="name",
	)
	if conflict:
		frappe.throw(
			_(
				"Another active Sales Order ({0}) already carries Wave Order ID {1}. "
				"Cancel that order first or use the existing record."
			).format(conflict, wave_order_id)
		)
