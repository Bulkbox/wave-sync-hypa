"""Compose Wave's OrderV3 POST body from an ERP Sales Order + Wave catalog data.

Three sources merged into the final body:

  * ERP SO header — name, customer notes, total -> integratorId, comments,
    totalPrice / orderItemsPrice.
  * ERP SO items — qty, rate, item_code -> products[].quantity / beginPrice /
    finalPrice / sku.
  * Wave admin product GET (one per distinct SKU) -> products[].name (localised),
    categories, uom, unitOfMeasurement, unitOfMeasurementBaseCoefficient,
    vat, isWeighed, stepToUom.

Pre-flight resolves every line's wave_product_id before any Wave HTTP fires.
If ANY line is unresolvable, the whole list is surfaced in one
WaveResolutionError so the caller can show all the broken SKUs to the
operator at once. No partial catalog GETs happen on a failing build.
"""

from __future__ import annotations

import frappe

from wave_sync_hypa.wave_sync_hypa.services import product_resolver, wave_client
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveOutboundError, WaveResolutionError
from wave_sync_hypa.wave_sync_hypa.utils.money import major_to_cents


def build_order_payload(sales_order, customer_id: str, settings, correlation_id: str, config: dict) -> dict:
	"""Return the OrderV3 dict to POST to /api/v3/admin/orders.

	`sales_order` is a Frappe Sales Order doc. `customer_id` is the resolved
	Wave-side userId. `config` carries the Wave HTTP credentials (base_url,
	api_key, app_id) used for the catalog GETs.
	"""
	line_resolutions = _resolve_lines(sales_order, settings, correlation_id)

	unresolvable = [r["item_code"] for r in line_resolutions if not r["wave_product_id"]]
	if unresolvable:
		raise WaveResolutionError(
			f"Cannot push to Wave — these items have no Wave product mapping: "
			f"{sorted(unresolvable)}. Open the Item, set wave_product_id manually, "
			"OR trigger a stock movement so the resolver finds it, "
			"OR remove the line from this Sales Order."
		)

	catalog_by_product_id = _fetch_catalogs(line_resolutions, config)

	return _assemble_body(sales_order, customer_id, settings, line_resolutions, catalog_by_product_id)


def _resolve_lines(sales_order, settings, correlation_id: str) -> list[dict]:
	"""Per product SO line, resolve wave_product_id; return resolution records.

	Fee/shipping lines (item codes configured in Wave Settings.fee_mappings) are
	skipped: they are charge items, not Wave catalog products, so they are never
	pushed to Wave's order products[] — and excluding them keeps a fee line from
	failing the resolve-everything pre-flight.
	"""
	fee_items = _fee_item_codes(settings)
	out: list[dict] = []
	for item in sales_order.get("items") or []:
		item_code = (item.get("item_code") or "").strip()
		if item_code in fee_items:
			continue
		wave_product_id = _resolve_product_id(item_code, settings, correlation_id)
		out.append({
			"item_code": item_code,
			"qty": float(item.get("qty") or 0),
			"rate": float(item.get("rate") or 0),
			"amount": float(item.get("amount") or 0),
			"wave_product_id": wave_product_id,
		})
	return out


def _fee_item_codes(settings) -> set[str]:
	"""ERP item codes mapped as Wave fees (shipping, bags, ...); never pushed as products."""
	return {
		(row.get("erp_item_code") or "").strip()
		for row in (settings.get("fee_mappings") or [])
		if (row.get("erp_item_code") or "").strip()
	}


def _resolve_product_id(item_code: str, settings, correlation_id: str) -> str | None:
	"""Return Item.wave_product_id from cache, falling back to the by-sku resolver."""
	if not item_code:
		return None
	cached = frappe.db.get_value("Item", item_code, "wave_product_id")
	if cached:
		return cached
	return product_resolver.resolve_wave_product_id(item_code, settings, correlation_id)


def _fetch_catalogs(line_resolutions: list[dict], config: dict) -> dict[str, dict]:
	"""GET each unique wave_product_id from Wave's admin catalog. 404 -> WaveOutboundError."""
	out: dict[str, dict] = {}
	for resolution in line_resolutions:
		product_id = resolution["wave_product_id"]
		if product_id in out:
			continue
		product = wave_client.get_admin_product_by_id(
			base_url=config["base_url"],
			api_key=config["api_key"],
			app_id=config["app_id"],
			product_id=product_id,
		)
		if product is None:
			raise WaveOutboundError(
				f"Wave product {product_id} returned 404 — cached wave_product_id is stale. "
				f"Clear Item.wave_product_id for SKU '{resolution['item_code']}' and retry.",
				http_status=404,
				wave_code="PRODUCT_NOT_FOUND",
			)
		out[product_id] = product
	return out


def _assemble_body(
	sales_order,
	customer_id: str,
	settings,
	line_resolutions: list[dict],
	catalog_by_product_id: dict[str, dict],
) -> dict:
	"""Compose the final OrderV3 dict from all three sources."""
	divisor = int(settings.get("price_scale_divisor") or 100)

	products = []
	for resolution in line_resolutions:
		catalog = catalog_by_product_id[resolution["wave_product_id"]]
		rate_cents = major_to_cents(resolution["rate"], divisor)
		products.append({
			"productId": resolution["wave_product_id"],
			"quantity": resolution["qty"],
			"beginPrice": rate_cents,
			"finalPrice": rate_cents,
			"sku": resolution["item_code"],
			"name": catalog.get("name") or [],
			"categories": catalog.get("categories") or [],
			"uom": catalog.get("uom") or [],
			"unitOfMeasurement": catalog.get("unitOfMeasurement") or [],
			"unitOfMeasurementBaseCoefficient": catalog.get("unitOfMeasurementBaseCoefficient") or 1,
			"vat": int(catalog.get("vat") or 0),
			"isWeighed": bool(catalog.get("isWeighed")),
			"stepToUom": catalog.get("stepToUom") or 1,
		})

	order_items_cents = sum(
		major_to_cents(resolution["amount"], divisor) for resolution in line_resolutions
	)

	payment_type = (settings.get("wave_default_offline_payment_type") or "cash").strip() or "cash"
	shop_id = (settings.get("wave_shop_id") or "").strip()

	return {
		"integratorId": sales_order.get("name"),
		"userId": customer_id,
		"shopId": shop_id,
		"products": products,
		"paymentType": payment_type,
		"paymentStatus": "PENDING",
		"status": "PENDING",
		"orderType": "ORDER",
		"totalPrice": order_items_cents,
		"orderItemsPrice": order_items_cents,
		"comments": _build_comments(sales_order),
		"paymentManagedByIntegrator": True,
		"deliveryService": "standard",
	}


def _build_comments(sales_order) -> str:
	"""Default comment for ERP-pushed orders — operator-readable provenance."""
	existing = (sales_order.get("wave_comments") or "").strip()
	if existing:
		return existing
	return f"ERP-pushed offline order ({sales_order.get('name')})."
