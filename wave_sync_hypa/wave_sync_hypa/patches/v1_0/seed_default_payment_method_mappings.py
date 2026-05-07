"""One-shot seed of the canonical Wave Payment Method Mapping rows.

Inserts one row per Wave paymentType value with the classification (prepaid|cod)
pre-filled. The mode_of_payment Link column is left blank because each site
names its Mode of Payment records differently (MPESA vs M-Pesa vs Mobile Money,
etc.) — operators point each row at an existing MOP after the seed runs.

Idempotent: keyed on wave_payment_type, only inserts rows whose paymentType
is not already in the table. Patches don't re-run, so an operator who later
deletes a default row is not re-imposed-upon.

Mirrors patches/v1_0/seed_default_outbound_rules.py exactly in shape.
"""

from __future__ import annotations

import frappe

DEFAULT_MAPPINGS = [
	{
		"wave_payment_type": "card",
		"classification": "prepaid",
		"description": "Online card via gateway (e.g. IPAYAFRICA). Money already collected at checkout.",
	},
	{
		"wave_payment_type": "klarna",
		"classification": "prepaid",
		"description": "Klarna BNPL — funds settled by Klarna before fulfilment.",
	},
	{
		"wave_payment_type": "mobile",
		"classification": "prepaid",
		"description": "Mobile money via gateway. Treated as prepaid because Wave reports the funds before the order lands.",
	},
	{
		"wave_payment_type": "bankTransfer",
		"classification": "prepaid",
		"description": "Bank transfer settled via gateway prior to Wave webhook.",
	},
	{
		"wave_payment_type": "thirdPartyReference",
		"classification": "prepaid",
		"description": "Third-party gateway reference (vouchers, partner wallets) — money already received.",
	},
	{
		"wave_payment_type": "cardOnDelivery",
		"classification": "cod",
		"description": "Card payment collected at the door by the rider.",
	},
	{
		"wave_payment_type": "irisOnDelivery",
		"classification": "cod",
		"description": "Iris (biometric) payment collected at delivery.",
	},
	{
		"wave_payment_type": "cash",
		"classification": "cod",
		"description": "Cash collected at the door.",
	},
]


def execute() -> None:
	"""Insert any default mapping whose wave_payment_type is missing."""
	settings = frappe.get_single("Wave Settings")
	existing = {
		(row.wave_payment_type or "").strip()
		for row in (settings.payment_method_mappings or [])
	}
	added = 0
	for default in DEFAULT_MAPPINGS:
		if default["wave_payment_type"] in existing:
			continue
		settings.append("payment_method_mappings", default)
		added += 1
	if not added:
		return
	# Bypass the always-protect child-table guard (this is a controlled seed).
	settings.flags.allow_child_table_clear = True
	settings.flags.ignore_validate = True
	settings.save(ignore_permissions=True)
	frappe.db.commit()
	frappe.clear_document_cache("Wave Settings", "Wave Settings")
