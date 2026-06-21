"""Mirror a Wave cash coupon onto an ERPNext Coupon Code (+ backing Pricing Rule).

Wave is the source of truth for the discount amount: the Coupon Code is created
on first sight, and on later orders its Pricing Rule's discount_amount is realigned
to whatever Wave now sends. Validity/usage stays with Wave — the Coupon Code is
given a very high maximum_use so a reused coupon never trips ERPNext's
exhaustion guard at Sales Order submit.

Cash (fixed-amount) coupons only. A coupon already configured in ERP as a
percentage rule is a mismatch this phase cannot reconcile and raises
WaveResolutionError so intake soft-fails + flags rather than mis-applying it.
"""

from __future__ import annotations

import frappe

from wave_sync_hypa.wave_sync_hypa.services.logger import log_step
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveResolutionError

# Coupon Code.used is compared against maximum_use on SO submit; Wave owns the
# real validity rules, so the cap is set high enough never to block a reused coupon.
COUPON_MAX_USE = 1_000_000_000

# Wave owns coupon validity; the ERP Pricing Rule must not impose its own date
# window. ERPNext defaults valid_from to "today", which would silently drop the
# discount on a back-dated or post-midnight order — so open the start date.
COUPON_RULE_VALID_FROM = "2000-01-01"

STEP_CREATED = "coupon_created"
STEP_REALIGNED = "coupon_amount_realigned"


def resolve_coupon(code: str, amount_major: float, settings, correlation_id: str) -> str:
	"""Return the ERP Coupon Code name for a Wave coupon, creating/realigning it.

	`amount_major` is Wave's discount already converted to major units. Raises
	WaveResolutionError on a config gap (no code, non-positive amount, or an
	existing non-cash coupon) or when creation fails, so the caller can soft-skip.
	"""
	code = (code or "").strip()
	if not code:
		raise WaveResolutionError("Wave coupon has no code.")
	if amount_major <= 0:
		raise WaveResolutionError(f"Wave coupon {code!r} has a non-positive amount {amount_major}.")

	existing = frappe.db.get_value(
		"Coupon Code", {"coupon_code": code}, ["name", "pricing_rule"], as_dict=True
	)
	if existing:
		_realign(existing, code, amount_major, settings, correlation_id)
		return existing["name"]
	return _create(code, amount_major, settings, correlation_id)


def _realign(existing: dict, code: str, amount_major: float, settings, correlation_id: str) -> None:
	"""Wave is source of truth: realign the existing coupon's Pricing Rule amount/base if it drifted."""
	rule = existing.get("pricing_rule")
	if not rule:
		raise WaveResolutionError(f"ERP Coupon Code {code!r} has no Pricing Rule.")
	current = frappe.db.get_value(
		"Pricing Rule", rule, ["rate_or_discount", "discount_amount", "apply_discount_on"], as_dict=True
	)
	if (current or {}).get("rate_or_discount") != "Discount Amount":
		raise WaveResolutionError(
			f"ERP Coupon {code!r} is not a fixed-amount (cash) coupon; cannot reconcile its value."
		)

	changes = {}
	if float(current.get("discount_amount") or 0) != float(amount_major):
		changes["discount_amount"] = amount_major
	target_on = _apply_discount_on(settings)
	if (current.get("apply_discount_on") or "") != target_on:
		changes["apply_discount_on"] = target_on
	if not changes:
		return
	frappe.db.set_value("Pricing Rule", rule, changes)
	log_step(
		correlation_id, STEP_REALIGNED, "Info",
		linked_doctype="Coupon Code", linked_docname=existing["name"],
		response_body={"pricing_rule": rule, **changes},
	)


def _create(code: str, amount_major: float, settings, correlation_id: str) -> str:
	"""Create the Pricing Rule then the Coupon Code that links it; both idempotent on retry."""
	# Resolve config before the try so a config gap surfaces as WaveResolutionError
	# directly, rather than being swallowed by the create-failure except below.
	apply_discount_on = _apply_discount_on(settings)
	currency = _currency(settings)
	try:
		rule = frappe.get_doc(
			{
				"doctype": "Pricing Rule",
				"title": f"Wave Coupon {code}",
				"apply_on": "Transaction",
				"price_or_product_discount": "Price",
				"rate_or_discount": "Discount Amount",
				"discount_amount": amount_major,
				"apply_discount_on": apply_discount_on,
				"valid_from": COUPON_RULE_VALID_FROM,
				"currency": currency,
				"selling": 1,
				"coupon_code_based": 1,
			}
		).insert(ignore_permissions=True)
		coupon = frappe.get_doc(
			{
				"doctype": "Coupon Code",
				"coupon_name": code,
				"coupon_code": code,
				"coupon_type": "Promotional",
				"maximum_use": COUPON_MAX_USE,
				"pricing_rule": rule.name,
			}
		).insert(ignore_permissions=True)
	except Exception as exc:
		frappe.log_error(
			title="wave_sync_hypa: coupon creation failed",
			message=f"Could not create ERP Coupon Code/Pricing Rule for Wave coupon {code!r}: {exc}",
		)
		raise WaveResolutionError(f"Could not create ERP coupon {code!r}: {exc}") from exc

	log_step(
		correlation_id, STEP_CREATED, "Success",
		linked_doctype="Coupon Code", linked_docname=coupon.name,
		response_body={"pricing_rule": rule.name, "discount_amount": amount_major},
	)
	return coupon.name


def _apply_discount_on(settings) -> str:
	"""Net Total / Grand Total base for the coupon's Pricing Rule, from Wave Settings (default Grand Total)."""
	return (settings.get("coupon_apply_discount_on") or "").strip() or "Grand Total"


def _currency(settings) -> str:
	"""Pricing Rule.currency is required; use the integration's default currency."""
	currency = (settings.get("default_currency") or "").strip()
	if not currency:
		raise WaveResolutionError("Wave Settings.default_currency is not set; cannot create a coupon Pricing Rule.")
	return currency
