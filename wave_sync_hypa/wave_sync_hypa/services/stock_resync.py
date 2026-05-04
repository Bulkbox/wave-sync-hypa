"""Coordinator for operator-triggered stock resyncs.

Three UI entry points (Wave Settings button, Item form button, Item list
bulk action) all call the same backend endpoint, which enqueues this
coordinator. The coordinator runs in the worker, iterates the eligible
item universe (full default-warehouse roster, or an explicit list), and
per-item enqueues `push_item_stock` — the same worker the live SLE pipeline
uses, so the per-item dedup window is shared.

Hard contract: this function must complete the loop even if individual
items fail to enqueue. A Redis blip on item N must not stop items N+1 ..
from being queued. Per-item enqueue is wrapped in try/except; failures
are logged and counted, the loop continues.
"""

from __future__ import annotations

from typing import Iterator

import frappe

from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step

PUSH_WORKER_DOTTED_PATH = "wave_sync_hypa.wave_sync_hypa.services.stock_pusher.push_item_stock"
RESYNC_JOB_NAME = "wave-sync:resync:full"
ITEM_CHUNK_SIZE = 500

STEP_RESYNC_REQUESTED = "stock_sync_resync_requested"
STEP_RESYNC_STARTED = "stock_sync_resync_started"
STEP_RESYNC_COMPLETED = "stock_sync_resync_completed"
STEP_RESYNC_ABORTED = "stock_sync_resync_aborted"
STEP_RESYNC_FAILED = "stock_sync_resync_failed"
STEP_RESYNC_ITEM_ENQUEUE_FAILED = "stock_sync_resync_item_enqueue_failed"


def enqueue_full_resync_jobs(batch_id: str, item_codes: list[str] | None = None) -> None:
	"""Worker entry point: fan out one push_item_stock job per eligible item; never raises."""
	try:
		_run_resync(batch_id, item_codes)
	except Exception as exc:
		log_step(
			correlation_id=batch_id,
			step=STEP_RESYNC_FAILED,
			level="Error",
			friendly_id=batch_id,
			error_message=f"resync coordinator crashed: {exc}",
			stack_trace=frappe.get_traceback(),
		)


def _run_resync(batch_id: str, item_codes: list[str] | None) -> None:
	"""Real coordinator body: validate settings, iterate items, enqueue per item."""
	settings = frappe.get_cached_doc("Wave Settings")
	if not settings.get("outbound_stock_sync_enabled"):
		log_step(
			correlation_id=batch_id,
			step=STEP_RESYNC_ABORTED,
			level="Error",
			friendly_id=batch_id,
			error_message="outbound_stock_sync_enabled flipped off after resync was queued.",
		)
		return

	warehouse = settings.get("default_warehouse")
	if not warehouse:
		log_step(
			correlation_id=batch_id,
			step=STEP_RESYNC_ABORTED,
			level="Error",
			friendly_id=batch_id,
			error_message="default_warehouse is not configured.",
		)
		return

	scope = "all" if item_codes is None else f"explicit:{len(item_codes)}"
	log_step(
		correlation_id=batch_id,
		step=STEP_RESYNC_STARTED,
		level="Info",
		friendly_id=batch_id,
		request_body={"warehouse": warehouse, "scope": scope},
	)

	queued, enqueue_failed = _enqueue_each_item(batch_id, warehouse, item_codes)

	log_step(
		correlation_id=batch_id,
		step=STEP_RESYNC_COMPLETED,
		level="Success",
		friendly_id=batch_id,
		request_body={"queued": queued, "enqueue_failed": enqueue_failed, "scope": scope},
	)


def _enqueue_each_item(batch_id: str, warehouse: str, item_codes: list[str] | None) -> tuple[int, int]:
	"""Iterate eligible items and enqueue one push job each; return (queued, enqueue_failed)."""
	queued = 0
	enqueue_failed = 0
	for item_code in _iter_eligible_item_codes(warehouse, item_codes):
		if _try_enqueue_one(item_code, batch_id):
			queued += 1
		else:
			enqueue_failed += 1
	return queued, enqueue_failed


def _try_enqueue_one(item_code: str, batch_id: str) -> bool:
	"""Enqueue a per-item push for one SKU; return True on success, False after logging failure."""
	correlation_id = new_correlation_id()
	try:
		frappe.enqueue(
			PUSH_WORKER_DOTTED_PATH,
			queue="default",
			job_id=f"wave-sync:stock:{item_code}",
			deduplicate=True,
			item_code=item_code,
			correlation_id=correlation_id,
			batch_id=batch_id,
		)
		return True
	except Exception as exc:
		log_step(
			correlation_id=correlation_id,
			step=STEP_RESYNC_ITEM_ENQUEUE_FAILED,
			level="Error",
			doc_type="Item",
			linked_doctype="Item",
			linked_docname=item_code,
			friendly_id=batch_id,
			error_message=f"failed to enqueue push for {item_code}: {exc}",
			stack_trace=frappe.get_traceback(),
		)
		return False


def _iter_eligible_item_codes(warehouse: str, item_codes: list[str] | None) -> Iterator[str]:
	"""Yield item_codes that should be resynced: enabled stock items, optionally restricted."""
	for chunk in _paginate_eligible_items(warehouse, item_codes):
		for row in chunk:
			yield row[0]


def _paginate_eligible_items(warehouse: str, item_codes: list[str] | None) -> Iterator[list[tuple]]:
	"""Page through Item rows in stable order; warehouse is reserved for future qty pre-checks."""
	offset = 0
	while True:
		chunk = _fetch_chunk(item_codes, offset, ITEM_CHUNK_SIZE)
		if not chunk:
			return
		yield chunk
		if len(chunk) < ITEM_CHUNK_SIZE:
			return
		offset += ITEM_CHUNK_SIZE


def _fetch_chunk(item_codes: list[str] | None, offset: int, limit: int) -> list[tuple]:
	"""Pull one page of (item_code,) rows for enabled stock items; optional explicit-name filter."""
	filters: dict = {"disabled": 0, "is_stock_item": 1}
	if item_codes is not None:
		filters["name"] = ["in", item_codes]
	return [
		(row["name"],)
		for row in frappe.get_all(
			"Item",
			filters=filters,
			fields=["name"],
			order_by="name asc",
			start=offset,
			page_length=limit,
		)
	]


def count_eligible_items(warehouse: str, item_codes: list[str] | None = None) -> int:
	"""Count the items a resync would queue; used at click time for the operator estimate."""
	filters: dict = {"disabled": 0, "is_stock_item": 1}
	if item_codes is not None:
		filters["name"] = ["in", item_codes]
	return frappe.db.count("Item", filters=filters)
