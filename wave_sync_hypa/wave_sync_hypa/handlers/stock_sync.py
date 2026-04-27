"""ERP-side hook: enqueue a Wave stock push when an SLE submits.

Fires on every Stock Ledger Entry submission (the single fan-in point for all
ERPNext stock movements). Filters to the configured default warehouse and
enqueues a deduplicated job per item — many SLEs touching the same item
collapse into one push that reads the latest Bin qty at execution time.

This module is intentionally chatty: every accepted, skipped, or rejected
SLE is logged to Wave Sync Log so operators can audit the chain end to end.
"""

from __future__ import annotations

import frappe

from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step

STEP_SKIPPED_DISABLED = "stock_sync_skipped_disabled"
STEP_SKIPPED_OTHER_WAREHOUSE = "stock_sync_skipped_other_warehouse"
STEP_SKIPPED_NO_WAREHOUSE_CONFIG = "stock_sync_skipped_no_default_warehouse"
STEP_SKIPPED_NO_ITEM_CODE = "stock_sync_skipped_no_item_code"
STEP_ENQUEUED = "stock_sync_enqueued"
STEP_ENQUEUE_FAILED = "stock_sync_enqueue_failed"

WORKER_DOTTED_PATH = "wave_sync_hypa.wave_sync_hypa.services.stock_pusher.push_item_stock"


def on_sle_submit(doc, method=None) -> None:
	"""doc_event entry point for Stock Ledger Entry.on_submit; enqueue or log a skip."""
	settings = frappe.get_cached_doc("Wave Settings")
	correlation_id = new_correlation_id()
	item_code = doc.get("item_code")
	warehouse = doc.get("warehouse")

	if not item_code:
		log_step(
			correlation_id=correlation_id,
			step=STEP_SKIPPED_NO_ITEM_CODE,
			level="Warning",
			doc_type="Stock Ledger Entry",
			linked_doctype="Stock Ledger Entry",
			linked_docname=doc.name,
			error_message="SLE has no item_code; nothing to push.",
		)
		return

	if not settings.get("outbound_stock_sync_enabled"):
		log_step(
			correlation_id=correlation_id,
			step=STEP_SKIPPED_DISABLED,
			level="Info",
			doc_type="Item",
			linked_doctype="Item",
			linked_docname=item_code,
			error_message="outbound_stock_sync_enabled is off; SLE not enqueued.",
		)
		return

	default_warehouse = settings.get("default_warehouse")
	if not default_warehouse:
		log_step(
			correlation_id=correlation_id,
			step=STEP_SKIPPED_NO_WAREHOUSE_CONFIG,
			level="Warning",
			doc_type="Item",
			linked_doctype="Item",
			linked_docname=item_code,
			error_message="Wave Settings.default_warehouse is not set; cannot decide whether to push.",
		)
		return

	if warehouse != default_warehouse:
		log_step(
			correlation_id=correlation_id,
			step=STEP_SKIPPED_OTHER_WAREHOUSE,
			level="Info",
			doc_type="Item",
			linked_doctype="Item",
			linked_docname=item_code,
			error_message=(
				f"SLE warehouse {warehouse!r} is not the configured default "
				f"{default_warehouse!r}; skipping."
			),
		)
		return

	_enqueue_push(item_code, doc.name, correlation_id)


def _enqueue_push(item_code: str, sle_name: str, correlation_id: str) -> None:
	"""Queue a deduplicated stock-push job for one item; log success or enqueue failure."""
	job_id = f"wave-sync:stock:{item_code}"
	try:
		frappe.enqueue(
			WORKER_DOTTED_PATH,
			queue="default",
			job_id=job_id,
			deduplicate=True,
			enqueue_after_commit=True,
			item_code=item_code,
			correlation_id=correlation_id,
			batch_id=None,
		)
	except Exception as exc:
		log_step(
			correlation_id=correlation_id,
			step=STEP_ENQUEUE_FAILED,
			level="Error",
			doc_type="Item",
			linked_doctype="Item",
			linked_docname=item_code,
			error_message=f"failed to enqueue stock push: {exc}",
			stack_trace=frappe.get_traceback(),
		)
		return

	log_step(
		correlation_id=correlation_id,
		step=STEP_ENQUEUED,
		level="Info",
		doc_type="Item",
		linked_doctype="Item",
		linked_docname=item_code,
		request_body={"job_id": job_id, "triggered_by_sle": sle_name},
	)
