"""Map a Wave product SKU to an ERPNext Item.

ERP is the inventory source of truth: every Wave item must already exist as
an ERP Item with a matching item_code. A missing SKU is a configuration gap,
not something the integration can invent on the fly.
"""

import frappe

from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveResolutionError


def resolve_sku(sku: str | None) -> str:
	"""Return the ERP Item code for the SKU; raise if absent or not sellable as-is.

	Rejecting disabled / template / non-sales items here lets the caller soft-skip
	them, instead of ERPNext throwing at Sales Order insert and dropping the order.
	"""
	if not sku:
		raise WaveResolutionError("SKU is empty in Wave payload")
	item = frappe.db.get_value(
		"Item", {"item_code": sku}, ["name", "disabled", "has_variants", "is_sales_item"], as_dict=True
	)
	if not item:
		raise WaveResolutionError(f"Item with SKU {sku!r} is not configured in ERP")
	if item.disabled:
		raise WaveResolutionError(f"Item with SKU {sku!r} is disabled in ERP")
	if item.has_variants:
		raise WaveResolutionError(f"Item with SKU {sku!r} is a template item; a variant is required")
	if not item.is_sales_item:
		raise WaveResolutionError(f"Item with SKU {sku!r} is not marked as a sales item")
	return item.name
