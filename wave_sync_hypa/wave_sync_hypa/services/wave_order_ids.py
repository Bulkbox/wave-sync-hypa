"""Shared primitives for resolving Wave order ids off ERP documents.

Each downstream doctype reaches a Wave-sourced Sales Order through a
different child table and link field — a Pick List via locations.sales_order,
a Delivery Note via items.against_sales_order, a Sales Invoice via
items.sales_order (then items.delivery_note), a Payment Entry via its
references[]. The per-doctype *walk* genuinely differs, so it stays in each
handler; what is mechanically identical — reading a child-row field,
dereferencing wave_order_id, and deduping while preserving order — lives here
so the handlers don't each re-copy it.
"""

from __future__ import annotations

import frappe


def child_row_field(row, fieldname: str) -> str:
	"""Read a field off a child row whether it's a Frappe doc, a _dict, or a plain dict."""
	if hasattr(row, "get"):
		return (row.get(fieldname) or "").strip()
	return (getattr(row, fieldname, "") or "").strip()


def wave_order_id_of(doctype: str, name: str) -> str:
	"""Return the doc's wave_order_id, or '' when the name is blank or the field is unset."""
	name = (name or "").strip()
	if not name:
		return ""
	return (frappe.db.get_value(doctype, name, "wave_order_id") or "").strip()


def dedupe_preserving_order(values) -> list[str]:
	"""Dedupe wave order ids, dropping blanks, preserving first-seen order."""
	seen: set[str] = set()
	out: list[str] = []
	for value in values:
		if value and value not in seen:
			seen.add(value)
			out.append(value)
	return out
