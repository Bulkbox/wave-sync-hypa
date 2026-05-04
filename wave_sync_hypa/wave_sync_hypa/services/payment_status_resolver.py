"""Decide COMPLETED vs PAYMENT_PENDING for one Wave order touched by a Payment Entry.

A PE can settle multiple Sales Invoices and/or Sales Orders across several
Wave orders. For each Wave order we look at the PE's references that target
that order and ask: are all of those docs fully settled now?

  * Every linked SI's post-submit outstanding_amount < 0.01   -> COMPLETED
  * Otherwise                                                  -> PAYMENT_PENDING

When the PE references Sales Orders directly (no SI exists yet — direct-on-SO
payment), fall back to comparing SO.advance_paid against SO.grand_total with
the same 1-cent tolerance. Mixed cases (some SI refs + some SO refs for the
same Wave order) require BOTH legs to be settled before reporting COMPLETED.

Pure: only `frappe.db.get_value` reads, no writes, no logging — easy to test.
"""

from __future__ import annotations

import frappe

# 1-cent tolerance — matches ERPNext's currency rounding convention and the
# credit-note classifier's full-value test.
FULL_PAYMENT_TOLERANCE = 0.01

STATUS_COMPLETED = "COMPLETED"
STATUS_PAYMENT_PENDING = "PAYMENT_PENDING"


def resolve_status_for_wave_order(pe_doc, wave_order_id: str) -> str:
	"""Return COMPLETED if every PE reference for this wave_order_id is fully settled, else PAYMENT_PENDING."""
	si_names, so_names = _references_for_wave_order(pe_doc, wave_order_id)
	if not si_names and not so_names:
		# No identifiable settlement target for this Wave order — be conservative.
		return STATUS_PAYMENT_PENDING
	if si_names and not _all_si_fully_paid(si_names):
		return STATUS_PAYMENT_PENDING
	if so_names and not _all_so_fully_advance_paid(so_names):
		return STATUS_PAYMENT_PENDING
	return STATUS_COMPLETED


def _references_for_wave_order(pe_doc, wave_order_id: str) -> tuple[list[str], list[str]]:
	"""Split the PE's references into (SI names, SO names) carrying the given wave_order_id."""
	si_names: list[str] = []
	so_names: list[str] = []
	for ref in pe_doc.get("references") or []:
		ref_doctype = _ref_field(ref, "reference_doctype")
		ref_name = _ref_field(ref, "reference_name")
		if not ref_doctype or not ref_name:
			continue
		if ref_doctype not in ("Sales Invoice", "Sales Order"):
			continue
		if frappe.db.get_value(ref_doctype, ref_name, "wave_order_id") != wave_order_id:
			continue
		(si_names if ref_doctype == "Sales Invoice" else so_names).append(ref_name)
	return si_names, so_names


def _ref_field(ref, fieldname: str) -> str:
	"""Read a field off a PE reference row whether it's a Frappe doc, a _dict, or a plain dict."""
	if hasattr(ref, "get"):
		return (ref.get(fieldname) or "").strip()
	return (getattr(ref, fieldname, "") or "").strip()


def _all_si_fully_paid(si_names: list[str]) -> bool:
	"""True iff every linked SI has post-submit outstanding_amount below the tolerance."""
	for si in si_names:
		outstanding = frappe.db.get_value("Sales Invoice", si, "outstanding_amount")
		if float(outstanding or 0) >= FULL_PAYMENT_TOLERANCE:
			return False
	return True


def _all_so_fully_advance_paid(so_names: list[str]) -> bool:
	"""True iff every linked SO's grand_total is fully covered by advance_paid."""
	for so in so_names:
		row = frappe.db.get_value(
			"Sales Order", so, ["grand_total", "advance_paid"], as_dict=True
		)
		if not row:
			return False
		total = float(row.get("grand_total") or 0)
		advance = float(row.get("advance_paid") or 0)
		if (total - advance) > FULL_PAYMENT_TOLERANCE:
			return False
	return True
