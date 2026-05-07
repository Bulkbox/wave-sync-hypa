"""One-shot seed of the "Wave Payment Validator Override" role.

This role bypasses the PE before_submit validator's hard-block branches:

  * mixed prepaid + COD references in one PE
  * prepaid PE with no Sales Invoice in references[]
  * prepaid PE with amount diverging from Wave's stamped paymentHold
  * COD PE with a non-COD-classified Mode of Payment

Override usage writes a `payment_validator_overridden` Warning audit row to
Wave Sync Log capturing user + which check was overridden + the original
diagnostic message. System Manager always passes the validator too.

Idempotent — only inserts if missing. Mirrors
seed_pick_list_wave_override_role.py exactly in shape.
"""

from __future__ import annotations

import frappe

PAYMENT_VALIDATOR_OVERRIDE_ROLE = "Wave Payment Validator Override"


def execute() -> None:
	"""Create the override role if it isn't already on the site."""
	if frappe.db.exists("Role", PAYMENT_VALIDATOR_OVERRIDE_ROLE):
		return
	frappe.get_doc({
		"doctype": "Role",
		"role_name": PAYMENT_VALIDATOR_OVERRIDE_ROLE,
		"desk_access": 1,
		"disabled": 0,
	}).insert(ignore_permissions=True)
	frappe.db.commit()
