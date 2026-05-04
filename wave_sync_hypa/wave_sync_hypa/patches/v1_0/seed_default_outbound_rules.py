"""One-shot seed of the canonical Wave Outbound Status Rules.

Patches run exactly once per site (tracked in tabPatch Log). This patch
inserts any of the default rule rows that the target site doesn't already
have, keyed on (erp_doctype, erp_event):

  * Sales Order  / cancel        -> CANCELLED
  * Pick List    / after_insert  -> ACCEPTED
  * Delivery Note/ submit        -> INVOICING
  * Sales Invoice/ submit        -> UNDER_DELIVERY  (only when is_return = 0)

Payment Entry is intentionally NOT in the pack: the PE handler computes the
target Wave status from paid vs outstanding (full -> COMPLETED, partial ->
PAYMENT_PENDING) and dispatches via forced_payload, bypassing the resolver.
A rule for PE would be misleading for operators reading the grid.

Idempotent within a single run, and because patches don't re-run, an operator
who deliberately deletes a default rule afterwards is NOT re-imposed-upon.
"""

from __future__ import annotations

import frappe

DEFAULT_RULES = [
	{
		"erp_doctype": "Sales Order",
		"erp_event": "cancel",
		"wave_status": "CANCELLED",
		"description": "Order outright cancelled in ERP -> mirror as terminal CANCELLED on Wave.",
	},
	{
		"erp_doctype": "Pick List",
		"erp_event": "after_insert",
		"wave_status": "ACCEPTED",
		"description": "Operator created a Pick List for this order -> Wave order moves to ACCEPTED.",
	},
	{
		"erp_doctype": "Delivery Note",
		"erp_event": "submit",
		"wave_status": "INVOICING",
		"description": "Goods dispatched via Delivery Note submit -> Wave order moves to INVOICING.",
	},
	{
		"erp_doctype": "Sales Invoice",
		"erp_event": "submit",
		"erp_condition_field": "is_return",
		"erp_condition_value": "0",
		"wave_status": "UNDER_DELIVERY",
		"description": "Invoice raised on a regular (non-return) SI -> Wave order moves to UNDER_DELIVERY.",
	},
]


def execute() -> None:
	"""Insert any default rule whose (erp_doctype, erp_event) is missing."""
	settings = frappe.get_single("Wave Settings")
	existing = {
		((row.erp_doctype or "").strip(), (row.erp_event or "").strip())
		for row in (settings.outbound_status_rules or [])
	}
	added = 0
	for default in DEFAULT_RULES:
		key = (default["erp_doctype"], default["erp_event"])
		if key in existing:
			continue
		settings.append("outbound_status_rules", {**default, "enabled": 1})
		added += 1
	if not added:
		return
	# Bypass the always-protect child-table guard (this is a controlled seed).
	settings.flags.allow_child_table_clear = True
	settings.flags.ignore_validate = True
	settings.save(ignore_permissions=True)
	frappe.db.commit()
	frappe.clear_document_cache("Wave Settings", "Wave Settings")
