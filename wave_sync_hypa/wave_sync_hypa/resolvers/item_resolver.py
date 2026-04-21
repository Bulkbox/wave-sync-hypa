"""Map a Wave product SKU to an ERPNext Item.

ERP is the inventory source of truth: every Wave item must already exist as
an ERP Item with a matching item_code. A missing SKU is a configuration gap,
not something the integration can invent on the fly.
"""

import frappe

from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveResolutionError


def resolve_sku(sku: str | None) -> str:
	"""Return the ERP Item code whose item_code equals the SKU; raise if absent."""
	if not sku:
		raise WaveResolutionError("SKU is empty in Wave payload")
	name = frappe.db.get_value("Item", {"item_code": sku}, "name")
	if not name:
		raise WaveResolutionError(
			f"Item with SKU {sku!r} is not configured in ERP"
		)
	return name
