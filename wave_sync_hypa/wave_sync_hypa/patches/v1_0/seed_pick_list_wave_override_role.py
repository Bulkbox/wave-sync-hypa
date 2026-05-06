"""One-shot seed of the "Pick List Wave Override" role.

This role is the explicit override for the Pick List submit/cancel lockdown
gate enforced in handlers/pick_list.py. The role exists to be assignable to
ops staff who need to manually submit Pick Lists when Wave's automation is
unavailable. System Manager always passes the gate too, but that role is
too privileged to be the everyday answer for warehouse leads — this
dedicated role is the one ops actually grants.

Patches run exactly once per site (tracked in tabPatch Log). Idempotent:
the role is created only when missing, and the patch never touches user
assignments — ops decides who gets the role post-migrate.
"""

from __future__ import annotations

import frappe

PICK_LIST_OVERRIDE_ROLE = "Pick List Wave Override"


def execute() -> None:
	"""Create the override role if it isn't already on the site."""
	if frappe.db.exists("Role", PICK_LIST_OVERRIDE_ROLE):
		return
	frappe.get_doc({
		"doctype": "Role",
		"role_name": PICK_LIST_OVERRIDE_ROLE,
		"desk_access": 1,
		"disabled": 0,
	}).insert(ignore_permissions=True)
	frappe.db.commit()
