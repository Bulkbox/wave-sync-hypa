"""Resolve a Wave customer _id for an offline Sales Order being pushed to Wave.

Two-branch resolution, no HTTP calls:

  1. The linked ERP Customer's wave_customer_id (cached from inbound
     CUSTOMER.UPDATE webhooks).
  2. Wave Settings.wave_common_offline_customer_id (the configured
     placeholder customer for ERP-pushed orders).

Both blank -> WaveResolutionError with an actionable message. The Wave-side
userSearchTerm endpoint is broken on dev, so email lookup is deferred; the
cached id from inbound webhooks is authoritative for customers Wave already
knows about. Operators stamp the field manually for the rare imported case.
"""

from __future__ import annotations

import frappe

from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveResolutionError


def resolve_wave_customer_for_so(sales_order, settings) -> str:
	"""Return a Wave customer _id; raise WaveResolutionError when nothing resolvable.

	`sales_order` may be a Frappe doc or a dict-like (tests pass SimpleNamespace).
	Only `customer` and `name` are read off it.
	"""
	customer = _so_field(sales_order, "customer")
	if customer:
		cached = (frappe.db.get_value("Customer", customer, "wave_customer_id") or "").strip()
		if cached:
			return cached

	default = (settings.get("wave_common_offline_customer_id") or "").strip()
	if default:
		return default

	so_name = _so_field(sales_order, "name") or "<unknown>"
	raise WaveResolutionError(
		f"No Wave customer mapping for Sales Order '{so_name}'. "
		f"Set wave_customer_id on Customer '{customer or '(unset)'}', "
		"OR configure Wave Settings → ERP → Wave Order Push → "
		"Common Offline Customer."
	)


def _so_field(so, fieldname: str) -> str:
	"""Read a field off the SO whether it's a Frappe doc, a _dict, or a plain dict."""
	if hasattr(so, "get") and not hasattr(so, fieldname):
		return (so.get(fieldname) or "").strip()
	return (getattr(so, fieldname, "") or "").strip()
