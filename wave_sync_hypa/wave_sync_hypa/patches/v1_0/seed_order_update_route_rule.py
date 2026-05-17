"""One-shot seed of the ORDER.UPDATE -> order_update route rule.

Routes Wave's ORDER.UPDATE webhook to handlers.order_update.handle so the
integration can reconcile ERP Pick Lists when Wave reports pickerStatus =
COLLECTED. Idempotent: only inserts the row when (ORDER, UPDATE) is not
already present in Wave Settings.route_rules.
"""

from __future__ import annotations

import frappe

ROUTE_DOC_TYPE = "ORDER"
ROUTE_ACTION = "UPDATE"
ROUTE_HANDLER_KEY = "order_update"


def execute() -> None:
	"""Insert the route rule when missing; leave operator-edited rows alone."""
	settings = frappe.get_single("Wave Settings")
	if _route_rule_exists(settings):
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


def _route_rule_exists(settings) -> bool:
	"""Return True when (ORDER, UPDATE) is already a row in Wave Settings.route_rules."""
	for row in settings.route_rules or []:
		if (row.doc_type or "").strip() == ROUTE_DOC_TYPE and (row.action or "").strip() == ROUTE_ACTION:
			return True
	return False
