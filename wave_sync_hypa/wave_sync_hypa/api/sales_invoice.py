"""HTTP surface for operator-facing Sales Invoice actions in the Wave Sync UI.

Kept separate from the inbound webhook layer and the Sales Invoice doc-event
handler (handlers/sales_invoice.py) so the operator-button endpoint has its own
home.
"""

import frappe

from wave_sync_hypa.wave_sync_hypa.services import prepaid_pe_creator
from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id


@frappe.whitelist()
def ensure_payment_entry(sales_invoice: str) -> dict:
	"""Operator-triggered: create/attach + submit the iPay Payment Entry for a prepaid SI.

	Invoked by the 'Wave Payment Entry' button. Runs the same idempotent engine
	the SI-submit worker uses (verify -> find-or-create draft -> attach -> submit),
	so it either confirms the existing Payment Entry or creates one when it is
	missing / a prior attempt errored. Returns a uniform envelope; never a 500 —
	the engine wraps ERPNext/validator throws into {ok: False, reason}.

	  {"ok": True,  "created": True/False, "payment_entry": "...", "reason": "...", "correlation_id": "..."}
	  {"ok": False, "created": ...,        "payment_entry": ...,   "reason": "...", "correlation_id": "..."}
	"""
	correlation_id = new_correlation_id()
	doc = frappe.get_doc("Sales Invoice", sales_invoice)
	doc.check_permission("submit")
	# No classification pre-check here: find_or_create_for_si resolves "prepaid"
	# authoritatively from the source Sales Order (via _prepaid_sources), so the
	# button works even for invoices whose mirrored field predates this feature.
	result = prepaid_pe_creator.find_or_create_for_si(sales_invoice, correlation_id)
	result["correlation_id"] = correlation_id
	frappe.db.commit()
	return result
