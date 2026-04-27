"""Flip unique=0 on Sales Order-wave_order_id; controller validation handles conditional uniqueness.

Background: the Custom Field was created on operator sites with unique=1 to
prevent two Sales Orders from sharing the same Wave Order ID. That database-
level constraint fights ERPNext's amend flow: when an operator cancels a
Wave-sourced SO and amends it, the new draft inherits wave_order_id from
its predecessor and the unique index rejects the save with a duplicate-key
error. Operators perceive this as "amends are broken."

The right semantics are conditional uniqueness:
  - No two ACTIVE (docstatus < 2) SOs may share a wave_order_id.
  - A cancelled SO does not block a new active one — that's the entire
    point of the cancel + amend workflow.

A database UNIQUE constraint cannot express "ignore cancelled rows"; we
move the check to the controller (handlers/sales_order_validation.py).
This patch drops the field-level unique flag so the index gets removed
on the next Custom Field save.
"""

import frappe

CUSTOM_FIELD_NAME = "Sales Order-wave_order_id"


def execute() -> None:
	"""Idempotent: flip Custom Field.unique to 0 on the wave_order_id field if not already."""
	if not frappe.db.exists("Custom Field", CUSTOM_FIELD_NAME):
		# Custom Field doesn't exist on this site (fresh install before fixtures ran).
		return
	cf = frappe.get_doc("Custom Field", CUSTOM_FIELD_NAME)
	if not cf.unique:
		return
	cf.unique = 0
	cf.save(ignore_permissions=True)
	frappe.db.commit()
