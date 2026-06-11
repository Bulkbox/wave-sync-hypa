"""Run inbound Wave processing as a configured integration user.

Inbound webhooks arrive in the Guest session (the endpoint is allow_guest), so
without this every Wave-created/modified record is attributed to Guest — a poor
audit trail. Wave Settings.wave_integration_user names a dedicated System User
to run the processing as instead; the records then read modified_by = that user.

`run_as_integration_user` is a no-op when the field is unset, so behaviour is
unchanged until an integration user is configured. The pick-list path passes
fallback="Administrator" so an unconfigured site keeps the previous behaviour
(it needs a real session user for a nested Warehouse read that ignore_permissions
does not cover).
"""

from __future__ import annotations

from contextlib import contextmanager

import frappe


def get_integration_user() -> str:
	"""Return the configured Wave integration user, or '' when unset.

	Reads through the cached Wave Settings single (the same accessor the handlers
	use) rather than db.get_single_value, whose cache-miss path can fan out to
	other queries.
	"""
	return (frappe.get_cached_doc("Wave Settings").get("wave_integration_user") or "").strip()


@contextmanager
def run_as_integration_user(*, ignore_permissions: bool = False, fallback: str | None = None):
	"""Run the enclosed block as the configured integration user (or `fallback`).

	No user switch happens when neither is set (current Guest behaviour). When
	`ignore_permissions` is True, frappe.flags.ignore_permissions is held on for
	the block — both the user and the flag are restored in the finally.
	"""
	user = get_integration_user() or fallback
	previous_user = frappe.session.user
	previous_flag = frappe.flags.get("ignore_permissions")
	switch = bool(user) and user != previous_user
	try:
		# Set the flag before switching user, matching the prior Administrator
		# context: set_user's internal reads run under ignore_permissions.
		if ignore_permissions:
			frappe.flags.ignore_permissions = True
		if switch:
			frappe.set_user(user)
		yield
	finally:
		if switch:
			frappe.set_user(previous_user)
		if ignore_permissions:
			frappe.flags.ignore_permissions = previous_flag
