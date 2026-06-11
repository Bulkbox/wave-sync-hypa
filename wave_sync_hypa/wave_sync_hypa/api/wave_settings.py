"""Operator-facing endpoints attached to Wave Settings.

Used by three buttons in the UI: Wave Settings (full warehouse resync),
Item form (single-SKU resync), and Item list view (selected-SKU resync).
All three converge on `start_full_resync` with an optional `item_codes`
list. The endpoint validates the click is safe to honor (admin only,
kill-switch on, all required outbound config present), allocates one
batch_id, enqueues the coordinator, and returns the batch_id so the UI
can show it in the toast and operators can filter Wave Sync Log by it.
"""

from __future__ import annotations

import frappe
from frappe import _

from wave_sync_hypa.wave_sync_hypa.services import stock_resync
from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step

REQUIRED_OUTBOUND_FIELDS = (
	"wave_api_base_url",
	"wave_app_id",
	"wave_store_id",
)


@frappe.whitelist()
def start_full_resync(item_codes: list[str] | str | None = None) -> dict:
	"""Validate, then queue a coordinator job that fans out per-item stock pushes."""
	frappe.only_for("System Manager")
	settings = frappe.get_doc("Wave Settings")
	_refuse_if_misconfigured(settings)
	codes = _normalise_item_codes(item_codes)
	batch_id = new_correlation_id()
	estimate = stock_resync.count_eligible_items(settings.default_warehouse, codes)
	_log_resync_requested(batch_id, settings.default_warehouse, codes, estimate)
	_enqueue_coordinator(batch_id, codes)
	return {
		"ok": True,
		"batch_id": batch_id,
		"item_count_estimate": estimate,
		"warehouse": settings.default_warehouse,
	}


def _refuse_if_misconfigured(settings) -> None:
	"""Throw with an operator-friendly message if the integration cannot make HTTP calls."""
	if not settings.get("enabled"):
		frappe.throw(_("Wave integration is disabled in Wave Settings; turn it on first."))
	if not settings.outbound_stock_sync_enabled:
		frappe.throw(_("Outbound Stock Sync is disabled in Wave Settings; turn it on first."))
	# Checked right after the enable switches and before the API credentials:
	# the resync only ever reads stock from this one warehouse, so a missing
	# warehouse is the operator's first thing to fix.
	if not (settings.get("default_warehouse") or "").strip():
		frappe.throw(_("Set a Default Warehouse in Wave Settings; stock is only ever synced from that warehouse."))
	for field in REQUIRED_OUTBOUND_FIELDS:
		value = settings.get(field)
		if isinstance(value, str):
			value = value.strip()
		if not value:
			frappe.throw(
				_("Wave Settings.{0} is required for outbound stock sync.").format(_(field))
			)
	if not settings.get_password("wave_api_key", raise_exception=False):
		frappe.throw(_("Wave Settings.wave_api_key is required for outbound stock sync."))


def _normalise_item_codes(raw: list[str] | str | None) -> list[str] | None:
	"""Accept None / JSON-string / list; return a clean list[str] or None for full mode."""
	if raw is None:
		return None
	if isinstance(raw, str):
		raw = frappe.parse_json(raw)
	if not isinstance(raw, list):
		frappe.throw(_("item_codes must be a list of Item names."))
	cleaned = [str(c).strip() for c in raw if c and str(c).strip()]
	if not cleaned:
		frappe.throw(_("Provide at least one Item name, or omit item_codes to resync everything."))
	return cleaned


def _log_resync_requested(batch_id: str, warehouse: str, codes: list[str] | None, estimate: int) -> None:
	"""Persist the click-time audit row so the run is traceable even before the worker picks it up."""
	scope = "all" if codes is None else f"explicit:{len(codes)}"
	log_step(
		correlation_id=batch_id,
		step=stock_resync.STEP_RESYNC_REQUESTED,
		level="Info",
		friendly_id=batch_id,
		request_body={
			"warehouse": warehouse,
			"scope": scope,
			"item_count_estimate": estimate,
			"requested_by": frappe.session.user,
		},
	)


def _enqueue_coordinator(batch_id: str, codes: list[str] | None) -> None:
	"""Queue the worker job that actually fans out per-item pushes."""
	frappe.enqueue(
		"wave_sync_hypa.wave_sync_hypa.services.stock_resync.enqueue_full_resync_jobs",
		queue="long",
		job_id=stock_resync.RESYNC_JOB_NAME,
		deduplicate=True,
		batch_id=batch_id,
		item_codes=codes,
	)
