"""Shared helpers for walking Payment Entry references[].

The PE handler's `on_payment_entry_submit` and the new `payment_validator`
both need to traverse a PE's references[] and dereference Wave fields off
the linked Sales Invoice / Sales Order rows. Keeping that walk in one
module ensures both paths agree on:

  - which reference doctypes carry wave_order_id (Sales Invoice + Sales Order),
  - what "child row" shape to support (Frappe doc, _dict, or plain dict).

Pure helpers, no I/O beyond a single frappe.db.get_value lookup per row.
"""

from __future__ import annotations

import frappe

from wave_sync_hypa.wave_sync_hypa.services.wave_order_ids import child_row_field

REFERENCE_DOCTYPES_WITH_WAVE_ID = ("Sales Invoice", "Sales Order")

# Re-exported under its historical name; the shared accessor lives in wave_order_ids.
# payment_validator imports `ref_field` from here.
ref_field = child_row_field


def collect_distinct_wave_order_ids(doc) -> list[str]:
	"""Return unique Wave order ids reachable from this PE's references[], in row order.

	Walks each reference row, filters to Sales Invoice / Sales Order, looks
	up wave_order_id, dedupes preserving encounter order. Other reference
	doctypes (Journal Entry, Expense Claim, etc.) are silently skipped.
	"""
	seen: set[str] = set()
	out: list[str] = []
	for ref in doc.get("references") or []:
		ref_doctype = ref_field(ref, "reference_doctype")
		ref_name = ref_field(ref, "reference_name")
		if ref_doctype not in REFERENCE_DOCTYPES_WITH_WAVE_ID or not ref_name:
			continue
		wave_order_id = (frappe.db.get_value(ref_doctype, ref_name, "wave_order_id") or "").strip()
		if wave_order_id and wave_order_id not in seen:
			seen.add(wave_order_id)
			out.append(wave_order_id)
	return out
