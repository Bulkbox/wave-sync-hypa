"""Append-only mapping of Wave addresses to ERPNext Address records.

The non-negotiable rule: never mutate an ERPNext Address that already carries
a `wave_address_id`. If Wave re-sends the same address `_id` we return the
existing ERP record unchanged. If Wave emits a new `_id` we insert a new
Address linked to the same Customer. This protects the customer's delivery
history and stops a Wave edit from silently rewriting an order-in-flight.
"""

import frappe

_TIMEZONE_TO_COUNTRY: dict[str, str] = {
	"Africa/Nairobi": "Kenya",
}

_WAVE_TYPE_TO_ERP: dict[str, str] = {
	"home": "Shipping",
	"work": "Office",
	"headquarters": "Office",
	"delivery": "Shipping",
	"billing": "Billing",
}


def append_if_new(customer_name: str, wave_address: dict) -> tuple[str, bool]:
	"""Return (address_name, created). Existing wave_address_id is returned unchanged."""
	wave_address_id = wave_address.get("_id")
	if not wave_address_id:
		return "", False

	existing = _find_by_wave_address_id(wave_address_id)
	if existing:
		return existing, False

	return _create_address(customer_name, wave_address), True


def _find_by_wave_address_id(wave_address_id: str) -> str | None:
	"""Return the ERPNext Address whose wave_address_id matches, or None."""
	return frappe.db.get_value("Address", {"wave_address_id": wave_address_id}, "name")


def _create_address(customer_name: str, wave_address: dict) -> str:
	"""Insert a new Address linked to the Customer; return the address name."""
	doc = frappe.get_doc(_build_address(customer_name, wave_address))
	doc.insert(ignore_permissions=True)
	return doc.name


def _build_address(customer_name: str, wave_address: dict) -> dict:
	"""Produce the frappe.get_doc input for a new Address from a Wave address dict."""
	return {
		"doctype": "Address",
		"address_title": f"{customer_name} - {wave_address.get('_id')}",
		"address_type": _map_type(wave_address.get("type")),
		"address_line1": _line1(wave_address),
		"address_line2": wave_address.get("notice") or None,
		"city": wave_address.get("city"),
		"pincode": wave_address.get("postalCode"),
		"country": _country(wave_address),
		"phone": wave_address.get("contactPhone"),
		"wave_address_id": wave_address.get("_id"),
		"links": [{"link_doctype": "Customer", "link_name": customer_name}],
	}


def _line1(wave_address: dict) -> str:
	"""Compose Address Line 1 from Wave's street + streetNo, trimmed."""
	parts = [wave_address.get("streetNo"), wave_address.get("street")]
	return " ".join(p for p in parts if p).strip() or "N/A"


def _map_type(wave_type: str | None) -> str:
	"""Translate a Wave address type to the closest ERPNext address_type Select value."""
	return _WAVE_TYPE_TO_ERP.get((wave_type or "").lower(), "Shipping")


def _country(wave_address: dict) -> str:
	"""Resolve the country: Wave `country` field, else from timeZone, else the ERP default."""
	explicit = wave_address.get("country")
	if explicit:
		return explicit
	mapped = _TIMEZONE_TO_COUNTRY.get(wave_address.get("timeZone") or "")
	if mapped:
		return mapped
	return _default_country()


def _default_country() -> str:
	"""Fallback country: the configured default Company's country, then Kenya."""
	company = frappe.db.get_single_value("Wave Settings", "default_company")
	if company:
		return frappe.db.get_value("Company", company, "country") or "Kenya"
	return "Kenya"
