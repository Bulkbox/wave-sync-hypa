"""Handle inbound ORDER.CREATE webhooks by drafting an ERPNext Sales Order.

Orchestration only. The handler resolves the customer, the shipping address,
each product SKU, and each fee mapping — then assembles a draft Sales Order
stamped with the Wave identifiers. Pricing for product lines is NOT passed;
the ERP Price List is the source of truth. Fee lines carry an explicit rate
because the fee amounts are variable per order and come from Wave in minor
units (cents).
"""

import frappe
from frappe.utils import escape_html, getdate

from wave_sync_hypa.wave_sync_hypa.resolvers.address_resolver import append_if_new
from wave_sync_hypa.wave_sync_hypa.resolvers.customer_resolver import find_or_create_customer
from wave_sync_hypa.wave_sync_hypa.resolvers.fee_resolver import resolve_fee
from wave_sync_hypa.wave_sync_hypa.resolvers.item_resolver import resolve_sku
from wave_sync_hypa.wave_sync_hypa.services.dispatcher import HANDLER_REGISTRY
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveResolutionError, WaveValidationError
from wave_sync_hypa.wave_sync_hypa.utils.money import cents_to_major

STEP_PAYMENT_METADATA_STAMPED = "payment_metadata_stamped"
STEP_PAYMENT_MAPPING_MISSING = "payment_method_mapping_missing"
STEP_PAYMENT_METADATA_ABSENT = "payment_metadata_absent"

# paymentStatus -> wave_payment_state for prepaid orders. Anything not in this
# map (or COMPLETED, handled separately) flags the SO for manual review.
_PREPAID_STATUS_TO_STATE = {
	"PENDING": "Pending",
	"FAILED": "Failed",
	"CANCELLED": "Refunded",
}


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
	skipped_tax = _apply_tax_template(sales_order, settings)
	skipped_items = _append_product_lines(sales_order, payload, correlation_id)
	skipped_fees = _append_fee_lines(sales_order, payload, settings)

	# Zero items resolved -> either fall back to a placeholder line or abort loudly.
	if not sales_order.items and not _append_placeholder_for_unresolved(sales_order, settings):
		_abort_intake_no_placeholder(payload, correlation_id, skipped_items)
		return

	_apply_payment_metadata(sales_order, settings, payload, correlation_id)
	_apply_wave_comments(sales_order, payload)
	_persist_sales_order(sales_order)

	if skipped_items:
		_annotate_sales_order_for_skipped_items(
			sales_order.name, payload, correlation_id, skipped_items
		)
	if skipped_fees:
		_annotate_sales_order_for_skipped_fees(
			sales_order.name, payload, correlation_id, skipped_fees
		)
	if skipped_tax:
		_annotate_sales_order_for_skipped_tax(
			sales_order.name, payload, correlation_id, skipped_tax
		)

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
	customer_name, _created, _source = find_or_create_customer(adapted)
	return customer_name


def _adapt_user_to_customer_payload(user: dict) -> dict:
	"""Translate an order's `user` sub-object to the shape find_or_create_customer expects.

	Forwards the B2B classification fields (customerType, companyName,
	businessType, fiscalId/taxId, businessAddress, city) so a customer first
	seen via an ORDER.CREATE webhook gets correctly classified instead of
	defaulting to Individual + default_customer_group. b2c payloads are
	unaffected — these fields will just be absent and the resolver returns
	the same values as before.
	"""
	return {
		"_id": user.get("_id"),
		"firstName": user.get("firstName"),
		"lastName": user.get("lastName"),
		"email": user.get("email"),
		"mobilePhone": user.get("mobile") or user.get("mobilePhone"),
		"isGuest": user.get("isGuest", False),
		"integratorId": user.get("integratorId"),
		"customerType": user.get("customerType"),
		"companyName": user.get("companyName"),
		"businessType": user.get("businessType"),
		"businessAddress": user.get("businessAddress"),
		"city": user.get("city"),
		"fiscalId": user.get("fiscalId"),
		"taxId": user.get("taxId"),
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


def _apply_tax_template(sales_order, settings) -> dict | None:
	"""Stamp the first enabled Wave Tax Rule's template on the SO; return a skip reason if none applied.

	ERPNext's selling controller copies the template's rows into `sales_order.taxes` at
	validate time, so we only need to set `taxes_and_charges` here. If the matched rule
	references a template that does not exist or is disabled we return a descriptor for
	the caller to annotate the SO with — we never block the order on a tax rule.
	"""
	for rule in settings.get("tax_rules") or []:
		if not rule.enabled:
			continue
		template = rule.sales_taxes_and_charges_template
		if not template:
			continue
		skip_reason = _validate_tax_template(template)
		if skip_reason:
			return {"template": template, "reason": skip_reason}
		sales_order.taxes_and_charges = template
		return None
	return None


def _validate_tax_template(template_name: str) -> str | None:
	"""Return a short reason string if the template is unusable; None if it can be applied."""
	if not frappe.db.exists("Sales Taxes and Charges Template", template_name):
		return "template_missing"
	if frappe.db.get_value("Sales Taxes and Charges Template", template_name, "disabled"):
		return "template_disabled"
	return None


def _append_product_lines(sales_order, payload: dict, correlation_id: str) -> list[dict]:
	"""Append resolvable Wave products as SO line items; return details of products that could not resolve.

	Mirrors _append_fee_lines: missing Item mappings are non-fatal here so an order
	with 1 unresolvable SKU out of 3 still produces a draft SO carrying the 2 that
	resolved, instead of vanishing silently. The caller decides what to do when the
	whole order failed to resolve (see _append_placeholder_for_unresolved).
	"""
	skipped: list[dict] = []
	for product in payload.get("products") or []:
		sku = (product.get("sku") or product.get("integratorId") or "").strip()
		try:
			item_code = resolve_sku(sku)
		except WaveResolutionError as exc:
			skipped.append(_unresolved_product_entry(sku, product, exc))
			continue
		sales_order.append("items", _build_item_line(item_code, product, sales_order))
	_log_items_resolved(correlation_id, payload, len(sales_order.items))
	return skipped


def _build_item_line(item_code: str, product: dict, sales_order) -> dict:
	"""Shape a Sales Order item dict from a resolved Wave product line."""
	return {
		"item_code": item_code,
		"qty": product.get("quantity") or 1,
		"delivery_date": sales_order.delivery_date,
	}


def _unresolved_product_entry(sku: str, product: dict, exc: Exception) -> dict:
	"""Capture an unresolvable Wave product line for downstream comments + ToDos."""
	return {
		"sku": sku,
		"quantity": product.get("quantity") or 1,
		"wave_product_id": product.get("productId"),
		"error": str(exc),
	}


def _append_placeholder_for_unresolved(sales_order, settings) -> bool:
	"""Append a single placeholder line when zero Wave items resolved; return False if no placeholder configured."""
	placeholder = (settings.get("default_unresolved_items_placeholder") or "").strip()
	if not placeholder or not frappe.db.exists("Item", placeholder):
		return False
	sales_order.append("items", {
		"item_code": placeholder,
		"qty": 1,
		"rate": 0,
		"delivery_date": sales_order.delivery_date,
		"description": "Wave order had no resolvable items — see Comments for the unresolved SKUs.",
	})
	return True


def _append_fee_lines(sales_order, payload: dict, settings) -> list[dict]:
	"""Add one SO item row per resolvable Wave fee; return details of fees that could not be resolved.

	Missing Fee Mappings are deliberately non-fatal here: the product lines are what
	drive fulfilment, and aborting the entire order because accounting has not yet
	added a mapping for a shipping fee would block the picker unnecessarily. Instead
	we collect the failures and let the caller annotate the Sales Order after save.
	"""
	divisor = int(settings.price_scale_divisor or 100)
	skipped: list[dict] = []
	for fee in payload.get("fees") or []:
		fee_type = fee.get("type")
		amount_cents = fee.get("amount")
		amount_major = cents_to_major(amount_cents, divisor)
		try:
			item_code = resolve_fee(fee_type)
		except WaveResolutionError as exc:
			skipped.append(
				{
					"type": fee_type,
					"amount_cents": amount_cents,
					"amount_major": amount_major,
					"error": str(exc),
				}
			)
			continue
		sales_order.append(
			"items",
			{
				"item_code": item_code,
				"qty": 1,
				"rate": amount_major,
				"delivery_date": sales_order.delivery_date,
			},
		)
	return skipped


def _apply_payment_metadata(sales_order, settings, payload: dict, correlation_id: str) -> None:
	"""Stamp Wave's payment metadata (paymentType, status, gateway, reference, hold) on the SO.

	Reads the Wave Payment Method Mapping table to translate paymentType into a
	classification (prepaid|cod) and to derive an operator-readable wave_payment_state.
	Amounts arrive in minor units (cents) and are converted to major units via
	cents_to_major(amount, settings.price_scale_divisor) so the validator at PE
	submit time can compare directly against pe.paid_amount in major units.

	Three cases:
	  - mapping found: stamp every field, set wave_payment_state from
	    classification + paymentStatus.
	  - mapping missing for this paymentType: stamp the raw fields anyway (so
	    operators can investigate), flag wave_manual_review_required, log Warning.
	  - paymentType absent from payload entirely: log Info, return.
	"""
	payment_type = (payload.get("paymentType") or "").strip()
	if not payment_type:
		log_step(
			correlation_id,
			STEP_PAYMENT_METADATA_ABSENT,
			"Info",
			doc_type="ORDER",
			action="CREATE",
			wave_id=payload.get("_id"),
			friendly_id=payload.get("friendlyId"),
			error_message="ORDER.CREATE payload has no paymentType; nothing to stamp.",
		)
		return

	divisor = int(settings.price_scale_divisor or 100)
	hold_major = cents_to_major(payload.get("paymentHold"), divisor)
	additional_hold_major = cents_to_major(
		(payload.get("additionalPaymentHold") or {}).get("amount"), divisor,
	)
	payment_status = (payload.get("paymentStatus") or "").strip()

	# Always stamp the raw observable fields so operators have something to go on
	# even when the mapping table is incomplete. Classification + state come from
	# the mapping; without it those two stay blank.
	sales_order.wave_payment_type = payment_type
	sales_order.wave_payment_status = payment_status or None
	sales_order.wave_payment_gateway = (payload.get("paymentGateway") or "").strip() or None
	sales_order.wave_payment_reference = (payload.get("paymentReference") or "").strip() or None
	sales_order.wave_payment_hold = hold_major
	sales_order.wave_additional_payment_hold = additional_hold_major

	mapping = _resolve_payment_method_mapping(settings, payment_type)
	if mapping is None:
		sales_order.wave_manual_review_required = 1
		log_step(
			correlation_id,
			STEP_PAYMENT_MAPPING_MISSING,
			"Warning",
			doc_type="ORDER",
			action="CREATE",
			wave_id=payload.get("_id"),
			friendly_id=payload.get("friendlyId"),
			error_message=(
				f"No Wave Payment Method Mapping for paymentType='{payment_type}'. "
				"Order metadata stamped but classification is unset; add a row in "
				"Wave Settings > Rules > Payment Method Mappings."
			),
		)
		return

	classification = (mapping.get("classification") or "").strip()
	sales_order.wave_payment_classification = classification or None
	sales_order.wave_payment_state = _derive_payment_state(classification, payment_status)
	if classification == "prepaid" and payment_status and payment_status != "COMPLETED":
		# Prepaid orders that aren't COMPLETED at intake (PENDING/FAILED/CANCELLED)
		# need an accountant to look at them.
		sales_order.wave_manual_review_required = 1

	log_step(
		correlation_id,
		STEP_PAYMENT_METADATA_STAMPED,
		"Info",
		doc_type="ORDER",
		action="CREATE",
		wave_id=payload.get("_id"),
		friendly_id=payload.get("friendlyId"),
		response_body={
			"payment_type": payment_type,
			"classification": classification,
			"payment_state": sales_order.wave_payment_state,
			"payment_hold_major": hold_major,
			"additional_hold_major": additional_hold_major,
		},
	)


def _resolve_payment_method_mapping(settings, payment_type: str) -> dict | None:
	"""Return the first mapping row whose wave_payment_type matches, else None."""
	for row in settings.get("payment_method_mappings") or []:
		if (row.get("wave_payment_type") or "").strip() == payment_type:
			return {
				"classification": row.get("classification"),
				"mode_of_payment": row.get("mode_of_payment"),
			}
	return None


def _derive_payment_state(classification: str, payment_status: str) -> str | None:
	"""Map (classification, paymentStatus) to the SO's wave_payment_state Select value."""
	if classification == "prepaid":
		if payment_status == "COMPLETED":
			return "Paid (Online)"
		return _PREPAID_STATUS_TO_STATE.get(payment_status, "Pending")
	if classification == "cod":
		return "Awaiting Cash on Delivery"
	return None


def _apply_wave_comments(sales_order, payload: dict) -> None:
	"""Stamp Wave's order-level `comments` (delivery notes, special requests) onto the SO."""
	sales_order.wave_comments = (payload.get("comments") or "").strip() or None


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


def _annotate_sales_order_for_skipped_items(
	sales_order_name: str,
	payload: dict,
	correlation_id: str,
	skipped: list[dict],
) -> None:
	"""Flag the SO for manual review, log each unresolved item, and attach a human-readable Comment."""
	_flag_manual_review(sales_order_name)
	_log_skipped_items(correlation_id, payload, sales_order_name, skipped)
	_add_skipped_items_comment(sales_order_name, payload, correlation_id, skipped)


def _log_skipped_items(
	correlation_id: str,
	payload: dict,
	sales_order_name: str,
	skipped: list[dict],
) -> None:
	"""Write one Action Required log row per unresolved Wave product."""
	for entry in skipped:
		log_step(
			correlation_id,
			"Action Required",
			"Warning",
			doc_type="ORDER",
			action="CREATE",
			wave_id=payload.get("_id"),
			wave_updated_at=payload.get("updatedAt"),
			friendly_id=payload.get("friendlyId"),
			linked_doctype="Sales Order",
			linked_docname=sales_order_name,
			error_message=(entry.get("error") or "")[:500],
			response_body={
				"sku": entry.get("sku"),
				"quantity": entry.get("quantity"),
				"wave_product_id": entry.get("wave_product_id"),
				"resolution": (
					"Add the missing Item in ERP, then either add the line to this Sales "
					"Order manually or cancel and re-trigger the ORDER webhook from Wave."
				),
			},
		)


def _add_skipped_items_comment(
	sales_order_name: str,
	payload: dict,
	correlation_id: str,
	skipped: list[dict],
) -> None:
	"""Attach a single Comment to the SO enumerating every unresolvable Wave SKU."""
	doc = frappe.get_doc("Sales Order", sales_order_name)
	doc.add_comment("Comment", _build_skipped_items_comment_html(payload, correlation_id, skipped))


def _build_skipped_items_comment_html(
	payload: dict,
	correlation_id: str,
	skipped: list[dict],
) -> str:
	"""Render the Comment body listing each unresolved SKU and the fix steps."""
	rows = "".join(_render_skipped_item_row(entry) for entry in skipped)
	return (
		"<div><b>Wave Sync &mdash; manual review required.</b></div>"
		f"<div>{len(skipped)} Wave product line(s) could not be added to this Sales "
		"Order because the SKU is not present (or is disabled) in ERP's Item master.</div>"
		f"<ul>{rows}</ul>"
		"<div><b>To resolve:</b></div>"
		"<ol>"
		"<li>Open <i>Item</i> and add (or enable) the missing SKU(s) above.</li>"
		"<li>Add the missing line(s) to this Sales Order manually with the quantities listed.</li>"
		"<li>Clear the <i>Wave Manual Review Required</i> flag on this Sales Order.</li>"
		"</ol>"
		f"<div><b>Wave order:</b> {escape_html(payload.get('friendlyId') or '—')} "
		f"(ID: {escape_html(payload.get('_id') or '—')})<br>"
		f"<b>Correlation:</b> {escape_html(correlation_id)}</div>"
	)


def _render_skipped_item_row(entry: dict) -> str:
	"""Render one <li> describing a single unresolved Wave SKU."""
	return (
		"<li><b>SKU {sku}</b> (qty: {qty}, Wave productId: {wave_product_id}): {error}</li>"
	).format(
		sku=escape_html(entry.get("sku") or "(unknown)"),
		qty=escape_html(str(entry.get("quantity") or 0)),
		wave_product_id=escape_html(entry.get("wave_product_id") or "—"),
		error=escape_html(entry.get("error") or ""),
	)


def _abort_intake_no_placeholder(
	payload: dict,
	correlation_id: str,
	skipped_items: list[dict],
) -> None:
	"""Log the intake abort when zero items resolved AND no placeholder Item is configured."""
	skus = ", ".join(e.get("sku") or "?" for e in skipped_items) or "(none reported)"
	error_message = (
		"All Wave product SKUs were unresolvable AND no "
		"Wave Settings.default_unresolved_items_placeholder is configured; "
		f"the Sales Order was NOT created. Unresolved SKUs: {skus}."
	)
	log_step(
		correlation_id,
		"Aborted",
		"Error",
		doc_type="ORDER",
		action="CREATE",
		wave_id=payload.get("_id"),
		wave_updated_at=payload.get("updatedAt"),
		friendly_id=payload.get("friendlyId"),
		error_message=error_message,
	)
	frappe.log_error(
		title="wave_sync_hypa: intake aborted — no placeholder Item configured",
		message=error_message,
	)


def _annotate_sales_order_for_skipped_fees(
	sales_order_name: str,
	payload: dict,
	correlation_id: str,
	skipped: list[dict],
) -> None:
	"""Flag the SO for manual review, log each skipped fee, and attach a human-readable Comment."""
	_flag_manual_review(sales_order_name)
	_log_skipped_fees(correlation_id, payload, sales_order_name, skipped)
	_add_skipped_fees_comment(sales_order_name, payload, correlation_id, skipped)


def _flag_manual_review(sales_order_name: str) -> None:
	"""Set wave_manual_review_required=1 via direct DB write so validate() does not re-run."""
	frappe.db.set_value(
		"Sales Order",
		sales_order_name,
		"wave_manual_review_required",
		1,
		update_modified=False,
	)
	frappe.db.commit()


def _log_skipped_fees(
	correlation_id: str,
	payload: dict,
	sales_order_name: str,
	skipped: list[dict],
) -> None:
	"""Write one Action Required log row per skipped fee with the type, amount, and resolver error."""
	for entry in skipped:
		log_step(
			correlation_id,
			"Action Required",
			"Warning",
			doc_type="ORDER",
			action="CREATE",
			wave_id=payload.get("_id"),
			wave_updated_at=payload.get("updatedAt"),
			friendly_id=payload.get("friendlyId"),
			linked_doctype="Sales Order",
			linked_docname=sales_order_name,
			error_message=(entry.get("error") or "")[:500],
			response_body={
				"wave_fee_type": entry.get("type"),
				"amount_cents": entry.get("amount_cents"),
				"amount_major": entry.get("amount_major"),
				"resolution": (
					"Add a row in Wave Settings > Rules > Fee Mappings for this fee type, "
					"then add the fee line to this Sales Order manually."
				),
			},
		)


def _add_skipped_fees_comment(
	sales_order_name: str,
	payload: dict,
	correlation_id: str,
	skipped: list[dict],
) -> None:
	"""Attach a single descriptive Comment to the Sales Order enumerating every skipped fee."""
	doc = frappe.get_doc("Sales Order", sales_order_name)
	doc.add_comment("Comment", _build_skipped_fees_comment_html(payload, correlation_id, skipped))


def _build_skipped_fees_comment_html(
	payload: dict,
	correlation_id: str,
	skipped: list[dict],
) -> str:
	"""Render the Comment body listing each skipped fee and the fix steps."""
	rows = "".join(_render_skipped_fee_row(entry) for entry in skipped)
	return (
		"<div><b>Wave Sync &mdash; manual review required.</b></div>"
		f"<div>{len(skipped)} fee line(s) could not be added to this Sales Order because their "
		"<i>Wave Fee Mapping</i> is missing or incomplete.</div>"
		f"<ul>{rows}</ul>"
		"<div><b>To resolve:</b></div>"
		"<ol>"
		"<li>Open <i>Wave Settings &rarr; Rules &rarr; Fee Mappings</i>.</li>"
		"<li>Add or complete the mapping for each Wave fee type listed above.</li>"
		"<li>Either add the missing fee line(s) to this Sales Order manually, "
		"or cancel and re-trigger the ORDER webhook from Wave.</li>"
		"<li>Clear the <i>Wave Manual Review Required</i> flag on this Sales Order.</li>"
		"</ol>"
		f"<div><b>Wave order:</b> {escape_html(payload.get('friendlyId') or '—')} "
		f"(ID: {escape_html(payload.get('_id') or '—')})<br>"
		f"<b>Correlation:</b> {escape_html(correlation_id)}</div>"
	)


def _render_skipped_fee_row(entry: dict) -> str:
	"""Render one <li> describing a single skipped fee."""
	return (
		"<li><b>{fee_type}</b> "
		"(amount: {amount_major:.2f}, cents: {amount_cents}): {error}</li>"
	).format(
		fee_type=escape_html(entry.get("type") or "(unknown)"),
		amount_major=float(entry.get("amount_major") or 0.0),
		amount_cents=escape_html(str(entry.get("amount_cents") or "—")),
		error=escape_html(entry.get("error") or ""),
	)


def _annotate_sales_order_for_skipped_tax(
	sales_order_name: str,
	payload: dict,
	correlation_id: str,
	skipped: dict,
) -> None:
	"""Flag the SO for manual review, log the skip, and attach a Comment describing the broken Tax Rule."""
	_flag_manual_review(sales_order_name)
	_log_skipped_tax(correlation_id, payload, sales_order_name, skipped)
	_add_skipped_tax_comment(sales_order_name, payload, correlation_id, skipped)


def _log_skipped_tax(
	correlation_id: str,
	payload: dict,
	sales_order_name: str,
	skipped: dict,
) -> None:
	"""Write one Action Required log row describing the broken Wave Tax Rule."""
	log_step(
		correlation_id,
		"Action Required",
		"Warning",
		doc_type="ORDER",
		action="CREATE",
		wave_id=payload.get("_id"),
		wave_updated_at=payload.get("updatedAt"),
		friendly_id=payload.get("friendlyId"),
		linked_doctype="Sales Order",
		linked_docname=sales_order_name,
		error_message=(
			f"Wave Tax Rule references template {skipped.get('template')!r} "
			f"({skipped.get('reason')}); no tax template applied to this SO."
		)[:500],
		response_body={
			"template": skipped.get("template"),
			"reason": skipped.get("reason"),
			"resolution": (
				"Open Wave Settings > Rules > Tax Rules, either replace or enable the "
				"referenced Sales Taxes and Charges Template, then set Taxes and Charges "
				"on this Sales Order manually or re-trigger the webhook."
			),
		},
	)


def _add_skipped_tax_comment(
	sales_order_name: str,
	payload: dict,
	correlation_id: str,
	skipped: dict,
) -> None:
	"""Attach a single descriptive Comment to the SO explaining why no tax template was applied."""
	doc = frappe.get_doc("Sales Order", sales_order_name)
	doc.add_comment("Comment", _build_skipped_tax_comment_html(payload, correlation_id, skipped))


def _build_skipped_tax_comment_html(payload: dict, correlation_id: str, skipped: dict) -> str:
	"""Render the Comment body explaining the broken Tax Rule and how to fix it."""
	return (
		"<div><b>Wave Sync &mdash; manual review required.</b></div>"
		"<div>No Sales Taxes and Charges Template was applied to this Sales Order "
		f"because the configured Wave Tax Rule references <b>{escape_html(skipped.get('template') or '—')}</b>, "
		f"which is <i>{escape_html(skipped.get('reason') or 'unusable')}</i>.</div>"
		"<div><b>To resolve:</b></div>"
		"<ol>"
		"<li>Open <i>Wave Settings &rarr; Rules &rarr; Tax Rules</i>.</li>"
		"<li>Replace the referenced template, or restore/enable it under "
		"<i>Sales Taxes and Charges Template</i>.</li>"
		"<li>Set <i>Taxes and Charges</i> on this Sales Order manually, or cancel "
		"and re-trigger the ORDER webhook from Wave.</li>"
		"<li>Clear the <i>Wave Manual Review Required</i> flag on this Sales Order.</li>"
		"</ol>"
		f"<div><b>Wave order:</b> {escape_html(payload.get('friendlyId') or '—')} "
		f"(ID: {escape_html(payload.get('_id') or '—')})<br>"
		f"<b>Correlation:</b> {escape_html(correlation_id)}</div>"
	)


# Register this handler into the dispatcher registry on module import.
HANDLER_REGISTRY["order_create"] = handle
