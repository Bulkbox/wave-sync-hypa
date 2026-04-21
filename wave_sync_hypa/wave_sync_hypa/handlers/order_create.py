"""Handle inbound ORDER.CREATE webhooks by drafting an ERPNext Sales Order.

Orchestration only. The handler resolves the customer, the shipping address,
each product SKU, and each fee mapping — then assembles a draft Sales Order
stamped with the Wave identifiers. Pricing for product lines is NOT passed;
the ERP Price List is the source of truth. Fee lines carry an explicit rate
because the fee amounts are variable per order and come from Wave in minor
units (cents).
"""

import frappe
from frappe.utils import getdate

from wave_sync_hypa.wave_sync_hypa.resolvers.address_resolver import append_if_new
from wave_sync_hypa.wave_sync_hypa.resolvers.customer_resolver import find_or_create_customer
from wave_sync_hypa.wave_sync_hypa.resolvers.fee_resolver import resolve_fee
from wave_sync_hypa.wave_sync_hypa.resolvers.item_resolver import resolve_sku
from wave_sync_hypa.wave_sync_hypa.services.dispatcher import HANDLER_REGISTRY
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveResolutionError, WaveValidationError
from wave_sync_hypa.wave_sync_hypa.utils.money import cents_to_major


def handle(payload: dict, correlation_id: str) -> None:
	"""Orchestrate ORDER.CREATE: dedup by wave_order_id, resolve parts, draft the Sales Order."""
	wave_order_id = _require(payload, "_id")

	existing = _find_existing_sales_order(wave_order_id)
	if existing:
		_log_skipped_existing(correlation_id, payload, existing)
		return

	settings = _load_settings()
	customer_name = _resolve_customer_for_order(payload)
	shipping_address = _resolve_shipping_address(customer_name, payload)

	sales_order = _build_sales_order_header(settings, customer_name, shipping_address, payload, correlation_id)
	_append_product_lines(sales_order, payload, correlation_id)
	_append_fee_lines(sales_order, payload, settings, correlation_id)
	_persist_sales_order(sales_order)

	_log_sales_order_created(correlation_id, payload, sales_order.name)


def _require(payload: dict, key: str):
	"""Return payload[key] or raise WaveValidationError when missing."""
	value = payload.get(key)
	if value is None or value == "":
		raise WaveValidationError(f"ORDER payload missing required field {key!r}")
	return value


def _find_existing_sales_order(wave_order_id: str) -> str | None:
	"""Return the ERP Sales Order name whose wave_order_id matches, or None."""
	return frappe.db.get_value("Sales Order", {"wave_order_id": wave_order_id}, "name")


def _load_settings():
	"""Return Wave Settings or raise if required ERP defaults are missing."""
	settings = frappe.get_cached_doc("Wave Settings")
	for field in ("default_company", "default_warehouse", "default_price_list", "default_currency"):
		if not settings.get(field):
			raise WaveValidationError(f"Wave Settings.{field} must be configured before order intake")
	return settings


def _resolve_customer_for_order(payload: dict) -> str:
	"""Resolve (or create) the ERP Customer from the order payload's `user` sub-object."""
	user = payload.get("user") or {}
	if not user.get("_id"):
		raise WaveResolutionError("ORDER payload missing user._id; cannot resolve customer")
	adapted = _adapt_user_to_customer_payload(user)
	customer_name, _created = find_or_create_customer(adapted)
	return customer_name


def _adapt_user_to_customer_payload(user: dict) -> dict:
	"""Translate an order's `user` sub-object to the shape find_or_create_customer expects."""
	return {
		"_id": user.get("_id"),
		"firstName": user.get("firstName"),
		"lastName": user.get("lastName"),
		"email": user.get("email"),
		"mobilePhone": user.get("mobile") or user.get("mobilePhone"),
		"isGuest": user.get("isGuest", False),
		"integratorId": user.get("integratorId"),
	}


def _resolve_shipping_address(customer_name: str, payload: dict) -> str | None:
	"""Resolve or append-create the order's delivery Address; return name or None if no address present."""
	wave_address = payload.get("address") or {}
	if not wave_address.get("_id"):
		return None
	address_name, _created = append_if_new(customer_name, wave_address)
	return address_name or None


def _build_sales_order_header(
	settings,
	customer_name: str,
	shipping_address: str | None,
	payload: dict,
	correlation_id: str,
):
	"""Return an unsaved Sales Order doc populated with header fields and the wave_* stamps."""
	return frappe.get_doc(
		{
			"doctype": "Sales Order",
			"customer": customer_name,
			"company": settings.default_company,
			"currency": settings.default_currency,
			"selling_price_list": settings.default_price_list,
			"set_warehouse": settings.default_warehouse,
			"transaction_date": _date_from_iso(payload.get("createdAt")) or getdate(),
			"delivery_date": _date_from_iso(
				payload.get("timeSlotStart") or payload.get("timeSlotEnd") or payload.get("createdAt")
			) or getdate(),
			"order_type": "Sales",
			"customer_address": shipping_address,
			"shipping_address_name": shipping_address,
			"wave_order_id": payload.get("_id"),
			"wave_friendly_id": payload.get("friendlyId"),
			"wave_status": payload.get("status"),
			"wave_correlation_id": correlation_id,
		}
	)


def _append_product_lines(sales_order, payload: dict, correlation_id: str) -> None:
	"""Add one Sales Order item row per Wave product; pricing auto-populates from the Price List."""
	for product in payload.get("products") or []:
		sku = product.get("sku") or product.get("integratorId")
		item_code = resolve_sku(sku)
		sales_order.append(
			"items",
			{
				"item_code": item_code,
				"qty": product.get("quantity") or 1,
				"delivery_date": sales_order.delivery_date,
			},
		)
	_log_items_resolved(correlation_id, payload, len(sales_order.items))


def _append_fee_lines(sales_order, payload: dict, settings, correlation_id: str) -> None:
	"""Add one Sales Order item row per Wave fee; rate is computed from the fee amount in cents."""
	divisor = int(settings.price_scale_divisor or 100)
	for fee in payload.get("fees") or []:
		fee_type = fee.get("type")
		item_code = resolve_fee(fee_type)
		sales_order.append(
			"items",
			{
				"item_code": item_code,
				"qty": 1,
				"rate": cents_to_major(fee.get("amount"), divisor),
				"delivery_date": sales_order.delivery_date,
			},
		)


def _persist_sales_order(sales_order) -> None:
	"""Insert the Sales Order as a draft, respecting permissions but skipping mandatory checks."""
	sales_order.flags.ignore_mandatory = True
	sales_order.insert(ignore_permissions=True)


def _date_from_iso(value: str | None):
	"""Convert an ISO datetime string to a date for SO header fields; return None if parse fails."""
	if not value:
		return None
	try:
		return getdate(value)
	except Exception:
		return None


def _log_skipped_existing(correlation_id: str, payload: dict, sales_order_name: str) -> None:
	"""Record a Skipped log row when a Sales Order for this wave_order_id already exists."""
	log_step(
		correlation_id,
		"Skipped",
		"Info",
		doc_type="ORDER",
		action="CREATE",
		wave_id=payload.get("_id"),
		wave_updated_at=payload.get("updatedAt"),
		friendly_id=payload.get("friendlyId"),
		linked_doctype="Sales Order",
		linked_docname=sales_order_name,
		response_body={"reason": "sales_order_already_exists"},
	)


def _log_items_resolved(correlation_id: str, payload: dict, count: int) -> None:
	"""Record a Resolved Items log row summarising how many product lines the handler appended."""
	log_step(
		correlation_id,
		"Resolved Items",
		"Info",
		doc_type="ORDER",
		action="CREATE",
		wave_id=payload.get("_id"),
		wave_updated_at=payload.get("updatedAt"),
		friendly_id=payload.get("friendlyId"),
		response_body={"product_line_count": count},
	)


def _log_sales_order_created(correlation_id: str, payload: dict, sales_order_name: str) -> None:
	"""Record an SO Created log row linking the Wave order to the new ERP Sales Order."""
	log_step(
		correlation_id,
		"SO Created",
		"Success",
		doc_type="ORDER",
		action="CREATE",
		wave_id=payload.get("_id"),
		wave_updated_at=payload.get("updatedAt"),
		friendly_id=payload.get("friendlyId"),
		linked_doctype="Sales Order",
		linked_docname=sales_order_name,
	)


# Register this handler into the dispatcher registry on module import.
HANDLER_REGISTRY["order_create"] = handle
