"""Idempotent seed of the CUSTOMER.UPDATE -> customer_upsert route rule."""

from __future__ import annotations

import frappe

ROUTE_DOC_TYPE = "CUSTOMER"
ROUTE_ACTION = "UPDATE"
ROUTE_HANDLER_KEY = "customer_upsert"


def execute() -> None:
	settings = frappe.get_single("Wave Settings")
	for row in settings.route_rules or []:
		if (row.doc_type or "").strip() == ROUTE_DOC_TYPE and (row.action or "").strip() == ROUTE_ACTION:
			return
	settings.append("route_rules", {
		"doc_type": ROUTE_DOC_TYPE,
		"action": ROUTE_ACTION,
		"handler_key": ROUTE_HANDLER_KEY,
		"enabled": 1,
	})
	settings.flags.allow_child_table_clear = True
	settings.flags.ignore_validate = True
	settings.save(ignore_permissions=True)
	frappe.db.commit()
	frappe.clear_document_cache("Wave Settings", "Wave Settings")
