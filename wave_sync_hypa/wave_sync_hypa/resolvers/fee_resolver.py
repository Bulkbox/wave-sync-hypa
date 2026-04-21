"""Map a Wave fee type (SHIPPING_COST, PLASTIC_BAGS, ...) to an ERP Item.

The mapping lives in `Wave Settings.fee_mappings` so admins can assign the
ERP Item used as a Sales Order line for each fee without a code change.
Missing mappings raise a resolution error — the integration will not silently
drop a fee from the order total.
"""

import frappe

from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveResolutionError


def resolve_fee(wave_fee_type: str | None) -> str:
	"""Return the ERP Item code mapped to this Wave fee type; raise if not mapped."""
	if not wave_fee_type:
		raise WaveResolutionError("Wave fee type is empty")
	settings = frappe.get_cached_doc("Wave Settings")
	for mapping in settings.get("fee_mappings") or []:
		if mapping.wave_fee_type == wave_fee_type:
			if not mapping.erp_item_code:
				raise WaveResolutionError(
					f"Wave Fee Mapping for {wave_fee_type!r} has no ERP Item configured"
				)
			return mapping.erp_item_code
	raise WaveResolutionError(f"No Wave Fee Mapping row found for {wave_fee_type!r}")
