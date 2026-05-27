"""Wipe wave_* fields on the amended copy of a cancelled Sales Order.

Frappe's amend flow ignores `no_copy=1` on Custom Fields by design — the
client-side copy_doc forces `is_no_copy=false` when `from_amend` is truthy,
so the fixture flag alone can't keep wave_* fields off the amended doc.
"""

from __future__ import annotations

import frappe

_WAVE_FIELDS_TO_WIPE_ON_AMEND = (
	"wave_order_id",
	"wave_friendly_id",
	"wave_status",
	"wave_correlation_id",
	"wave_origin",
	"wave_manual_review_required",
	"wave_push_failure_required_review",
	"wave_delivery_type",
	"wave_payment_classification",
	"wave_payment_state",
	"wave_payment_type",
	"wave_payment_status",
	"wave_payment_gateway",
	"wave_payment_reference",
	"wave_payment_hold",
	"wave_additional_payment_hold",
	"wave_comments",
)


def wipe_wave_fields_on_amend(doc, method=None) -> None:
	"""before_insert (Sales Order only): blank wave_* fields on amended docs."""
	if doc.doctype != "Sales Order":
		return
	if not doc.get("amended_from"):
		return

	# po_no is a standard ERPNext field (not no_copy) so it survives amend by
	# default. Clear it ONLY when it matches the friendly id we'd have stamped
	# during intake / push, so an operator-set paper PO is preserved.
	friendly = (doc.get("wave_friendly_id") or "").strip()
	po_no = (doc.get("po_no") or "").strip()

	for field in _WAVE_FIELDS_TO_WIPE_ON_AMEND:
		doc.set(field, None)

	if friendly and po_no == friendly:
		doc.po_no = None
