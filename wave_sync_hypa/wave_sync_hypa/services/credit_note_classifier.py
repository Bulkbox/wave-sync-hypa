"""Decide whether a return Sales Invoice cancels its original at full value.

A Credit Note in ERPNext is a Sales Invoice with `is_return=1` and
`return_against=<original_sales_invoice>` carrying a negative grand_total.
For Wave we treat:

  * abs(credit.grand_total) == original.grand_total  ->  full-value return
                                                          ==> push CANCELLED to Wave
  * abs(credit.grand_total) <  original.grand_total  ->  partial return
                                                          ==> no Wave push
                                                          (Phase 8 Payment
                                                           Entry reconciliation
                                                           will handle the
                                                           PAYMENT_PENDING side)
  * is_return=0 / no return_against / original missing -> not a Credit Note
                                                          we know how to
                                                          classify ==> False

Comparison uses a 1-cent tolerance to absorb rounding (rounding_adjustment
on Sales Invoice can introduce sub-cent differences when tax / discount
templates round differently between the original and the credit note).
"""

from __future__ import annotations

import frappe

# 1 cent tolerance — matches ERPNext's currency rounding convention.
FULL_VALUE_TOLERANCE = 0.01


def is_full_value_credit_note(doc) -> bool:
	"""Return True only when this Sales Invoice is a Credit Note that fully reverses its source.

	Caller contract: doc is a Sales Invoice (or a doc-like with .get(...)).
	A True return means the Wave-side order should be cancelled. False
	means either it's not a credit note at all, or it's a partial credit
	note that should NOT cancel the Wave order. Logging happens in the
	caller; this function is pure for easy unit testing.
	"""
	if not doc.get("is_return"):
		return False
	return_against = (doc.get("return_against") or "").strip()
	if not return_against:
		# is_return without return_against is a malformed credit note —
		# ERPNext usually validates this, but we defensively treat it as
		# non-classifiable rather than risk a wrong CANCELLED push.
		return False
	original_total = frappe.db.get_value("Sales Invoice", return_against, "grand_total")
	if original_total is None:
		# Source invoice was deleted or never persisted; cannot classify.
		return False
	credit_total_abs = abs(float(doc.get("grand_total") or 0))
	original_total_abs = abs(float(original_total))
	return abs(credit_total_abs - original_total_abs) < FULL_VALUE_TOLERANCE
