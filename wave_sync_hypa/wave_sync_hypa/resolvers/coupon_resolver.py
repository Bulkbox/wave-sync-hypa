"""Look up whether a Wave coupon code already exists as an ERPNext Coupon Code.

Wave owns coupons: we never create, realign, or apply them in ERP. A coupon on a
Wave order is a review signal for the team — this module only answers "does this
code already exist in ERP?" so intake can raise the right alarm.
"""

from __future__ import annotations

import frappe


def find_coupon_code(code: str) -> str | None:
	"""Return the ERP Coupon Code name matching this Wave coupon code, or None."""
	code = (code or "").strip()
	if not code:
		return None
	return frappe.db.get_value("Coupon Code", {"coupon_code": code}, "name")
