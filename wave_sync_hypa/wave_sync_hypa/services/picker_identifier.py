"""Single source of truth for what the picker app scans per Pick List line.

`Wave Settings.picker_identifier_source` is a Select with three semantic states
(blank, "Item Code", "Item Barcode"). The two functions in this module read
that single field and produce the right identifier list in each direction:

  outbound: ERP -> Wave   what we PATCH onto the Wave order's batchIds
  inbound:  Wave -> ERP   what we expect Wave's picker to echo back

Keeping both sides in one module guarantees they can never diverge. Wave's
payload shape (productId + batchIds) is identical across modes; only the
meaning of each identifier value shifts.
"""

from __future__ import annotations

import frappe


SOURCE_ITEM_CODE = "Item Code"
SOURCE_ITEM_BARCODE = "Item Barcode"


def _row_field(row, fieldname: str) -> str:
	"""Read a field off a child row whether it's a Frappe doc, a _dict, or a plain dict."""
	if hasattr(row, "get") and not hasattr(row, fieldname):
		return (row.get(fieldname) or "").strip()
	return (getattr(row, fieldname, "") or "").strip()


def identifiers_for_sku_outbound(rows: list, settings) -> list[str]:
	"""Return the identifier list to send to Wave for one SKU's Pick List rows.

	Three branches mirror the Select options:

	  * blank          -> batch_no per row (today's behaviour; preserves ERPNext's FEFO).
	  * "Item Code"    -> [item_code] (single element, consolidates rows for that SKU).
	  * "Item Barcode" -> [Item.barcodes[0].barcode] (single element); raises
	                       ValidationError when the Item has no barcode rows so
	                       the misconfiguration is loud, not silent.

	Empty / duplicate batch_no values in the blank-mode branch are dropped /
	deduped in encounter order — consistent with the existing batch_pusher
	helper that the worker already runs.
	"""
	source = _read_source(settings)
	sku = _row_field(rows[0], "item_code") if rows else ""

	if source == SOURCE_ITEM_CODE:
		return [sku]

	if source == SOURCE_ITEM_BARCODE:
		barcode = _first_item_barcode(sku)
		if not barcode:
			frappe.throw(
				f"Item {sku} has no Barcode row, but Wave Settings → Picker "
				f"Identifier Source is '{SOURCE_ITEM_BARCODE}'. Add a row to the "
				"Item's Barcodes child table, or switch the setting."
			)
		return [barcode]

	# Blank: today's batch-id behaviour — one entry per row, deduped.
	out: list[str] = []
	for row in rows:
		batch = _row_field(row, "batch_no")
		if batch and batch not in out:
			out.append(batch)
	return out


def identifier_matches_inbound(wave_id: str, rows: list, settings) -> bool:
	"""Return True when Wave's reported identifier matches what we sent outbound.

	Symmetric to `identifiers_for_sku_outbound`. The three branches mirror the
	Select options:

	  * blank          -> must equal one of the rows' batch_no values.
	  * "Item Code"    -> must equal the SKU itself.
	  * "Item Barcode" -> must equal the first Item Barcode row's value.

	An empty `wave_id` (Wave didn't report a batch / barcode at all) is treated
	as a match in every mode — the caller decides whether to require an
	identifier separately. This is intentional: some Wave deployments may not
	include batchIds on every payload, and we don't want to flag those as
	disparities when the picker app simply didn't surface the field.
	"""
	if not wave_id:
		return True

	source = _read_source(settings)
	sku = _row_field(rows[0], "item_code") if rows else ""

	if source == SOURCE_ITEM_CODE:
		return wave_id == sku

	if source == SOURCE_ITEM_BARCODE:
		expected = _first_item_barcode(sku)
		return bool(expected) and wave_id == expected

	# Blank: any of the SKU's allocated batch numbers.
	row_batches = {_row_field(r, "batch_no") for r in rows}
	row_batches.discard("")
	return wave_id in row_batches


def _read_source(settings) -> str:
	"""Pull the source string off Wave Settings; treat None / missing as blank."""
	return (settings.get("picker_identifier_source") or "").strip()


def _first_item_barcode(item_code: str) -> str | None:
	"""Return the first (by idx) Item Barcode row's barcode value, or None when absent."""
	if not item_code:
		return None
	rows = frappe.get_all(
		"Item Barcode",
		filters={"parent": item_code, "parenttype": "Item"},
		fields=["barcode"],
		order_by="idx asc",
		limit=1,
	)
	if not rows:
		return None
	return (rows[0].get("barcode") or "").strip() or None
