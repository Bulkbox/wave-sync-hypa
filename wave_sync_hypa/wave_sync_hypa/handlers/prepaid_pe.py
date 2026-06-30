"""Thin glue that enqueues the prepaid Payment Entry engine.

All business logic lives in services.prepaid_pe_creator; these functions only
gate (feature flag + master switch + prepaid classification) and enqueue, so
the doc-event hooks and call sites stay declarative.
"""

from __future__ import annotations

import frappe

from wave_sync_hypa.wave_sync_hypa.services import prepaid_pe_creator
from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id
from wave_sync_hypa.wave_sync_hypa.services.master_switch import skip_if_disabled


def enqueue_draft_on_so_submit(doc, method=None) -> None:
	"""Sales Order.on_submit: queue the unallocated draft PE for a confirmed prepaid order."""
	if (doc.get("wave_payment_classification") or "") != "prepaid":
		return
	maybe_enqueue_draft_for_order(doc.name)


def maybe_enqueue_draft_for_order(sales_order: str) -> None:
	"""Queue the draft PE for a Sales Order (e.g. after a successful manual iPay verify)."""
	cid = _gated_correlation_id("Sales Order", "prepaid_payment_entry_draft", sales_order)
	if cid:
		prepaid_pe_creator.enqueue_draft_for_order(sales_order, cid)


def maybe_enqueue_attach_for_si(sales_invoice: str) -> None:
	"""Queue the attach-and-submit for a submitted prepaid Sales Invoice."""
	cid = _gated_correlation_id("Sales Invoice", "prepaid_payment_entry", sales_invoice)
	if cid:
		prepaid_pe_creator.enqueue_attach_for_si(sales_invoice, cid)


def _gated_correlation_id(doctype: str, action: str, docname: str) -> str | None:
	"""A correlation id when the feature flag AND master switch both allow it, else None."""
	if not frappe.get_cached_doc("Wave Settings").get("ipay_auto_create_payment_entry"):
		return None
	correlation_id = new_correlation_id()
	if skip_if_disabled(correlation_id, doc_type=doctype, action=action, linked_doctype=doctype, linked_docname=docname):
		return None
	return correlation_id
