"""Create Frappe ToDos so the team sees intake issues without Wave Sync Log access.

Gated by Wave Settings.wave_intake_review_todo_enabled (default off). Recipients
are resolved User > Role; when neither is configured we silently no-op so a
half-configured site doesn't break intake. Two public entry points:

  notify_sales_order_needs_review(sales_order, settings, issue_summary)
      One ToDo per recipient, linked to the Sales Order (Medium priority).

  notify_intake_aborted(settings, payload, issue_summary)
      Standalone ToDo (no SO to link to), High priority — the integration
      could not create a record at all and ops must act before more orders
      land in the same hole.
"""

from __future__ import annotations

import frappe


def notify_sales_order_needs_review(sales_order, settings, issue_summary: str) -> int:
	"""Create Medium-priority ToDos linked to an SO that needs review; return count created."""
	if not _todo_enabled(settings):
		return 0
	recipients = _resolve_recipients(settings)
	if not recipients:
		return 0
	description = _build_sales_order_description(sales_order, issue_summary)
	return _create_todos(recipients, description, "Medium", "Sales Order", sales_order.name)


def notify_intake_aborted(settings, payload: dict, issue_summary: str) -> int:
	"""Create High-priority standalone ToDos when no Sales Order exists; return count created."""
	if not _todo_enabled(settings):
		return 0
	recipients = _resolve_recipients(settings)
	if not recipients:
		return 0
	description = _build_aborted_description(payload, issue_summary)
	return _create_todos(recipients, description, "High", None, None)


def _todo_enabled(settings) -> bool:
	"""True when the master switch on Wave Settings is on."""
	return bool(settings.get("wave_intake_review_todo_enabled"))


def _resolve_recipients(settings) -> list[str]:
	"""Return active User names to assign ToDos to; assignee wins over role."""
	assignee = (settings.get("wave_intake_review_assignee") or "").strip()
	if assignee:
		return _user_if_active(assignee)
	role = (settings.get("wave_intake_review_role") or "").strip()
	if role:
		return _active_users_in_role(role)
	return []


def _user_if_active(user: str) -> list[str]:
	"""Wrap a single User in a list when they exist and are enabled, else empty."""
	if not frappe.db.exists("User", user):
		return []
	if not frappe.db.get_value("User", user, "enabled"):
		return []
	return [user]


def _active_users_in_role(role: str) -> list[str]:
	"""Return enabled User names that carry this Role."""
	rows = frappe.get_all(
		"Has Role",
		filters={"role": role, "parenttype": "User"},
		fields=["parent"],
	)
	return [
		r["parent"] for r in rows
		if frappe.db.get_value("User", r["parent"], "enabled")
	]


def _create_todos(
	recipients: list[str],
	description: str,
	priority: str,
	reference_type: str | None,
	reference_name: str | None,
) -> int:
	"""Insert one ToDo per recipient; return how many were created."""
	created = 0
	for user in recipients:
		frappe.get_doc({
			"doctype": "ToDo",
			"allocated_to": user,
			"description": description,
			"priority": priority,
			"reference_type": reference_type,
			"reference_name": reference_name,
		}).insert(ignore_permissions=True)
		created += 1
	return created


def _build_sales_order_description(sales_order, issue_summary: str) -> str:
	"""Render a short ToDo body pointing at the SO needing review."""
	return (
		f"Wave Sync intake review needed for Sales Order {sales_order.name}: "
		f"{issue_summary}. Open the Sales Order and resolve the items listed in its Comments."
	)


def _build_aborted_description(payload: dict, issue_summary: str) -> str:
	"""Render a ToDo body for the intake-aborted case (no SO exists)."""
	friendly = payload.get("friendlyId") or payload.get("_id") or "(unknown)"
	return (
		f"Wave Sync could not create a Sales Order for Wave order {friendly}: "
		f"{issue_summary}. Check Wave Sync Log for details, configure the missing "
		"defaults in Wave Settings, then re-trigger the ORDER webhook from Wave."
	)
