"""Payment Entry hooks: stamp wave_order_id and push the computed status to Wave.

Wired in hooks.py:

  Payment Entry.validate    -> stamp_wave_order_id
  Payment Entry.on_submit   -> on_payment_entry_submit

A Payment Entry can settle multiple Sales Invoices and/or Sales Orders via
its `references` child table. Each row carries (reference_doctype,
reference_name); both Sales Invoice and Sales Order have the wave_order_id
Custom Field, so we walk the references, dereference each one's
wave_order_id, dedupe in encounter order, and dispatch one push per distinct
Wave order.

Status decision is computed (full vs partial settlement) and so cannot be
expressed in the rule schema's row-level field equality. We use the same
forced_payload escape hatch the credit-note classifier uses, with the
status string coming from payment_status_resolver. Each Wave order may
land on a different status (one fully paid, another partial), so we
dispatch per-Wave-order rather than fanning out a single payload.

Refunds (payment_type=Pay) are skipped at the handler level — they don't
correspond to a Wave-meaningful state transition. Cancellation of a PE is
intentionally NOT wired: COMPLETED is terminal in Wave's enum, and any
backward jump is rejected (ORDER0049, soft-skipped).
"""

from __future__ import annotations

import frappe

from wave_sync_hypa.wave_sync_hypa.handlers import order_status
from wave_sync_hypa.wave_sync_hypa.services import payment_status_resolver, payment_validator
from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step
from wave_sync_hypa.wave_sync_hypa.services.pe_references import (
	collect_distinct_wave_order_ids as _collect_distinct_wave_order_ids,
)

STEP_STAMP_MULTI_SOURCE = "payment_entry_wave_order_id_multi_source"
STEP_SKIPPED_PAYMENT_TYPE = "payment_entry_skipped_non_receive_payment_type"


def stamp_wave_order_id(doc, method=None) -> None:
	"""validate hook: copy the first reachable wave_order_id onto the PE for filterability.

	Idempotent: skips when wave_order_id is already set. Multi-source PEs
	(references reaching multiple distinct Wave orders) get a Warning row
	enumerating every id so the audit trail is unambiguous.
	"""
	if doc.get("wave_order_id"):
		return
	wave_ids = _collect_distinct_wave_order_ids(doc)
	if not wave_ids:
		return
	doc.wave_order_id = wave_ids[0]
	if len(wave_ids) > 1:
		log_step(
			correlation_id=new_correlation_id(),
			step=STEP_STAMP_MULTI_SOURCE,
			level="Warning",
			doc_type=doc.doctype,
			linked_doctype=doc.doctype,
			linked_docname=doc.name or "<new>",
			wave_id=wave_ids[0],
			request_body={"wave_order_ids": wave_ids},
			error_message=(
				f"Payment Entry references {len(wave_ids)} distinct Wave-sourced documents. "
				"Stamping the first on wave_order_id; status push will fan out to all."
			),
		)


def on_payment_entry_submit(doc, method=None) -> None:
	"""on_submit hook: per Wave order, compute COMPLETED vs PAYMENT_PENDING and dispatch."""
	if (doc.get("payment_type") or "").strip() != "Receive":
		log_step(
			correlation_id=new_correlation_id(),
			step=STEP_SKIPPED_PAYMENT_TYPE,
			level="Info",
			doc_type=doc.doctype,
			linked_doctype=doc.doctype,
			linked_docname=doc.name,
			error_message=(
				f"Payment Entry payment_type={doc.get('payment_type')!r}; only 'Receive' "
				"PEs push status to Wave (refunds are out of scope)."
			),
		)
		return

	wave_ids = _collect_distinct_wave_order_ids(doc)
	if not wave_ids and doc.get("wave_order_id"):
		wave_ids = [doc.wave_order_id]
	for wave_order_id in wave_ids:
		status = payment_status_resolver.resolve_status_for_wave_order(doc, wave_order_id)
		order_status.dispatch_with_wave_order_ids(
			doc,
			"submit",
			[wave_order_id],
			forced_payload={"status": status},
		)


def validate_payment_before_submit(doc, method=None) -> None:
	"""before_submit hook: delegate to payment_validator; raises ValidationError on hard-block branches.

	Pure thin wrapper kept here so hooks.py points at a handler module like
	the rest of the integration. The actual logic lives in
	services/payment_validator.py.
	"""
	payment_validator.validate_pe_before_submit(doc)


# _collect_distinct_wave_order_ids is imported from services.pe_references above.
# The validator (services.payment_validator) imports the same helper, so both
# code paths share one definition of "which references count as Wave-sourced".
