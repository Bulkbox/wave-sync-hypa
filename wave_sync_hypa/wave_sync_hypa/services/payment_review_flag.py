"""Flag a Sales Order / Sales Invoice for the accounting team when an iPay
payment cannot be verified, and clear the flag once it can.

Two visible signals, both idempotent:
  * the `wave_payment_review_required` Check + `wave_payment_review_reason`
    Small Text on the doc (set via db.set_value so it works on submitted
    docs too, and so it doesn't re-run validate); these drive the form
    banner and a list-view standard filter for accounting; and
  * a Frappe ToDo assigned to `Wave Settings.wave_payment_review_assignee`,
    so the team sees it in "My Tasks" without Wave Sync Log access — same
    escalation idea as intake_review_notifier, but a distinct (accounting)
    recipient and its own marker so the two flows don't close each other's
    ToDos.

Never raises: a flag/clear failure must not break the SO/SI/PE flow that
triggered it.
"""

from __future__ import annotations

import frappe

from wave_sync_hypa.wave_sync_hypa.services.logger import log_step

# Prefix on the ToDo description so we can find + close exactly the ToDos this
# module created, without disturbing intake-review or push-failure ToDos.
PAYMENT_REVIEW_TODO_MARKER = "Wave Sync — payment verification review"

STEP_FLAGGED = "payment_review_flagged"
STEP_CLEARED = "payment_review_cleared"


def flag(doctype: str, name: str, reason: str, *, settings=None, correlation_id: str = "") -> None:
	"""Set the payment-review flag + reason on the doc and raise one accounting ToDo. Never raises."""
	try:
		frappe.db.set_value(
			doctype,
			name,
			{"wave_payment_review_required": 1, "wave_payment_review_reason": reason},
			update_modified=False,
		)
		settings = settings or frappe.get_cached_doc("Wave Settings")
		_raise_todo(doctype, name, reason, settings)
		log_step(
			correlation_id=correlation_id,
			step=STEP_FLAGGED,
			level="Warning",
			doc_type=doctype,
			linked_doctype=doctype,
			linked_docname=name,
			error_message=reason,
		)
	except Exception as exc:
		log_step(
			correlation_id=correlation_id,
			step=STEP_FLAGGED,
			level="Error",
			doc_type=doctype,
			linked_doctype=doctype,
			linked_docname=name,
			error_message=f"failed to flag {doctype} {name} for payment review: {exc}",
			stack_trace=frappe.get_traceback(),
		)


def clear(doctype: str, name: str, *, settings=None, correlation_id: str = "") -> None:
	"""Clear the payment-review flag + reason and close any open accounting ToDos. Never raises."""
	try:
		frappe.db.set_value(
			doctype,
			name,
			{"wave_payment_review_required": 0, "wave_payment_review_reason": None},
			update_modified=False,
		)
		_close_todos(doctype, name)
		log_step(
			correlation_id=correlation_id,
			step=STEP_CLEARED,
			level="Info",
			doc_type=doctype,
			linked_doctype=doctype,
			linked_docname=name,
		)
	except Exception as exc:
		log_step(
			correlation_id=correlation_id,
			step=STEP_CLEARED,
			level="Error",
			doc_type=doctype,
			linked_doctype=doctype,
			linked_docname=name,
			error_message=f"failed to clear payment review on {doctype} {name}: {exc}",
			stack_trace=frappe.get_traceback(),
		)


def _raise_todo(doctype: str, name: str, reason: str, settings) -> None:
	"""Create one open ToDo for the accounting assignee; no-op if none set or one already open."""
	assignee = (settings.get("wave_payment_review_assignee") or "").strip()
	if not assignee or not _is_active_user(assignee):
		return
	if _open_todo_exists(doctype, name, assignee):
		return
	frappe.get_doc({
		"doctype": "ToDo",
		"allocated_to": assignee,
		"description": (
			f"{PAYMENT_REVIEW_TODO_MARKER} — {doctype} {name}: {reason}. "
			"Verify the iPay payment, then reconcile / create the Payment Entry."
		),
		"priority": "High",
		"reference_type": doctype,
		"reference_name": name,
	}).insert(ignore_permissions=True)


def _close_todos(doctype: str, name: str) -> None:
	"""Close every open payment-review ToDo referencing this doc."""
	rows = frappe.get_all(
		"ToDo",
		filters={
			"reference_type": doctype,
			"reference_name": name,
			"status": "Open",
			"description": ("like", f"{PAYMENT_REVIEW_TODO_MARKER}%"),
		},
		pluck="name",
	)
	for todo in rows:
		frappe.db.set_value("ToDo", todo, "status", "Closed", update_modified=False)


def _open_todo_exists(doctype: str, name: str, assignee: str) -> bool:
	"""True when an open payment-review ToDo for this doc + assignee already exists (dedup)."""
	return bool(frappe.db.exists(
		"ToDo",
		{
			"reference_type": doctype,
			"reference_name": name,
			"allocated_to": assignee,
			"status": "Open",
			"description": ("like", f"{PAYMENT_REVIEW_TODO_MARKER}%"),
		},
	))


def _is_active_user(user: str) -> bool:
	"""True when the User exists and is enabled."""
	return bool(frappe.db.exists("User", user)) and bool(frappe.db.get_value("User", user, "enabled"))
