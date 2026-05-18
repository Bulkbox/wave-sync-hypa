"""One-shot seed of the default `Shipping Cost` Item + matching Fee Mapping row.

A new Wave Sync deployment needs an ERP Item to receive the SHIPPING_COST fee
line at intake. This patch creates a sensible default so a fresh site has a
working pipeline immediately; operators can rename / re-point / delete later
and the patch never re-imposes.

The Item is non-stock (it's a charge, not inventory). No Item Tax Template is
attached at Item level — the per-line override comes from
Wave Settings.shipping_item_tax_template at intake time. Operators who use the
Item on manual (non-Wave) Sales Orders can attach a default template via the
Item form if they want.

Three steps, each idempotent:

  1. Create the Item if it doesn't already exist.
  2. Append SHIPPING_COST -> <Item> to Wave Settings.fee_mappings if the row
     keyed on SHIPPING_COST is missing.

Steps run only when the prerequisite is present (e.g. an Item Group must
exist before we can create the Item).
"""

from __future__ import annotations

import frappe

SHIPPING_ITEM_CODE = "Shipping Cost"
WAVE_FEE_TYPE = "SHIPPING_COST"


def execute() -> None:
	"""Seed the Shipping Cost Item + Fee Mapping row; idempotent on every run."""
	item_code = _ensure_shipping_cost_item()
	if not item_code:
		return
	_ensure_fee_mapping_row(item_code)


def _ensure_shipping_cost_item() -> str | None:
	"""Create the `Shipping Cost` Item once; return its name, or None when site validators reject it."""
	if frappe.db.exists("Item", SHIPPING_ITEM_CODE):
		return SHIPPING_ITEM_CODE
	item_group = _default_item_group()
	if not item_group:
		return None
	try:
		doc = frappe.get_doc({
			"doctype": "Item",
			"item_code": SHIPPING_ITEM_CODE,
			"item_name": SHIPPING_ITEM_CODE,
			"item_group": item_group,
			"stock_uom": "Nos",
			"is_stock_item": 0,
			"is_purchase_item": 1,
			"include_item_in_manufacturing": 0,
			"description": (
				"Shipping fee line used by Wave Sync for SHIPPING_COST payload fees. "
				"Non-stock (no inventory). Per-order rate is set on the SO line at intake "
				"from Wave's payload; tax-adjusted via Wave Settings.shipping_item_tax_template."
			),
		})
		doc.flags.ignore_mandatory = True
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name
	except Exception as exc:
		# Compliance apps (e.g. kenya_compliance_via_slade) add controller-level
		# validations that bypass ignore_mandatory and demand site-specific fields
		# (KRA Country of Origin, Item Type, etc.). Log + bail; migrate stays green.
		frappe.db.rollback()
		frappe.log_error(
			title="wave_sync_hypa: could not auto-create Shipping Cost Item",
			message=(
				f"Attempted to seed the default Shipping Cost Item but the insert was rejected: {exc}. "
				"Create a shipping Item manually (with whatever fields your compliance app needs), "
				"then add a SHIPPING_COST -> <your Item> row in Wave Settings > Rules > Fee Mappings."
			),
		)
		return None


def _ensure_fee_mapping_row(item_code: str) -> None:
	"""Append SHIPPING_COST -> item_code to Wave Settings.fee_mappings when missing."""
	settings = frappe.get_single("Wave Settings")
	if _fee_mapping_exists(settings, WAVE_FEE_TYPE):
		return
	settings.append("fee_mappings", {
		"wave_fee_type": WAVE_FEE_TYPE,
		"erp_item_code": item_code,
		"description": (
			"Wave shipping fee. Per-order rate is variable; the intake handler "
			"sets it on the SO line from payload.fees[].amount, optionally "
			"back-calculated via Wave Settings.shipping_item_tax_template."
		),
	})
	# Bypass the always-protect child-table guard (this is a controlled seed).
	settings.flags.allow_child_table_clear = True
	settings.flags.ignore_validate = True
	settings.save(ignore_permissions=True)
	frappe.db.commit()
	frappe.clear_document_cache("Wave Settings", "Wave Settings")


def _fee_mapping_exists(settings, wave_fee_type: str) -> bool:
	"""Return True when fee_mappings already has a row for this wave_fee_type."""
	for row in settings.get("fee_mappings") or []:
		if (row.wave_fee_type or "").strip() == wave_fee_type:
			return True
	return False


def _default_item_group() -> str | None:
	"""Return a non-group Item Group name to assign the seeded Item to."""
	return (
		frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		or "All Item Groups"
	)
