"""Upsert ERPNext Addresses from Wave address payloads; soft-delete on Wave absence.

Wave keeps `_id` stable through customer-side edits — same id, mutated fields.
Lookup by `wave_address_id`: match + content diff overwrites the managed fields
in place and logs the diff; match + identical content is a no-op; no match
inserts. `address_title` is operator territory and never touched.

Delete propagation: when a CUSTOMER.UPDATE payload omits a `wave_address_id`
the ERP previously knew about for that customer, we soft-delete the ERP
Address — set `disabled = 1` and remove the Customer Dynamic Link row.
Historical SO/DN/SI pointers stay intact (the Address record survives).

The trade-off is deliberate: mid-flight Wave edits will mutate the underlying
Address that an open SO points at. ERPNext snapshots `address_display` onto the
SO at validate, so rendered/printed addresses stay frozen; only the live
record changes — which is what pickers and drivers actually want.
"""

import frappe

from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step

STEP_ADDRESS_UPSERT_UPDATED = "address_upsert_updated"
STEP_ADDRESS_UNLINKED_ON_WAVE_DELETE = "address_unlinked_on_wave_delete"

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

_MANAGED_FIELDS = (
	"address_type",
	"address_line1",
	"address_line2",
	"city",
	"pincode",
	"country",
	"phone",
)


def append_if_new(
	customer_name: str,
	wave_address: dict,
	correlation_id: str = "",
) -> tuple[str, bool]:
	"""Return (address_name, created). Created=True only when a new row is inserted."""
	wave_address_id = wave_address.get("_id")
	if not wave_address_id:
		return "", False

	existing = _find_by_wave_address_id(wave_address_id)
	if existing:
		_apply_updates_if_changed(existing, wave_address, correlation_id)
		return existing, False

	return _create_address(customer_name, wave_address), True


def disable_addresses_missing_from_payload(
	customer_name: str,
	payload_wave_ids: set[str],
	correlation_id: str = "",
) -> list[str]:
	"""Soft-delete ERP Addresses linked to this Customer whose wave_address_id is no longer in the payload.

	'Soft-delete' = `disabled = 1` + remove the Customer Dynamic Link row.
	The Address itself survives so historical SO/DN/SI pointers still resolve;
	only the active-address surface on the Customer card stops showing the
	disabled rows. Returns the list of disabled address names for caller logging.
	"""
	erp_addresses = frappe.db.sql(
		"""
		SELECT a.name, a.wave_address_id
		FROM `tabAddress` a
		JOIN `tabDynamic Link` dl ON dl.parent = a.name
		WHERE dl.link_doctype = 'Customer'
		  AND dl.link_name = %(customer)s
		  AND a.wave_address_id IS NOT NULL
		  AND a.wave_address_id <> ''
		  AND a.disabled = 0
		""",
		{"customer": customer_name},
		as_dict=True,
	)
	disabled: list[str] = []
	corr = correlation_id or new_correlation_id()
	for row in erp_addresses:
		if row.wave_address_id in payload_wave_ids:
			continue
		frappe.db.set_value("Address", row.name, "disabled", 1, update_modified=False)
		frappe.db.sql(
			"""
			DELETE FROM `tabDynamic Link`
			WHERE parent = %(addr)s
			  AND link_doctype = 'Customer'
			  AND link_name = %(customer)s
			""",
			{"addr": row.name, "customer": customer_name},
		)
		log_step(
			correlation_id=corr,
			step=STEP_ADDRESS_UNLINKED_ON_WAVE_DELETE,
			level="Info",
			doc_type="Address",
			linked_doctype="Address",
			linked_docname=row.name,
			request_body={"wave_address_id": row.wave_address_id, "customer": customer_name},
		)
		disabled.append(row.name)
	return disabled


def _find_by_wave_address_id(wave_address_id: str) -> str | None:
	return frappe.db.get_value("Address", {"wave_address_id": wave_address_id}, "name")


def _apply_updates_if_changed(
	address_name: str,
	wave_address: dict,
	correlation_id: str,
) -> None:
	"""Diff managed fields; overwrite + log when any differ. No-op when in sync."""
	incoming = _managed_payload(wave_address)
	current = (
		frappe.db.get_value("Address", address_name, list(incoming.keys()), as_dict=True)
		or {}
	)
	diff = [
		{"field": field, "before": current.get(field) or "", "after": incoming[field]}
		for field in incoming
		if (current.get(field) or "") != incoming[field]
	]
	if not diff:
		return

	for change in diff:
		frappe.db.set_value(
			"Address",
			address_name,
			change["field"],
			change["after"],
			update_modified=False,
		)
	log_step(
		correlation_id=correlation_id or new_correlation_id(),
		step=STEP_ADDRESS_UPSERT_UPDATED,
		level="Info",
		doc_type="Address",
		linked_doctype="Address",
		linked_docname=address_name,
		request_body={"wave_address_id": wave_address.get("_id"), "diff": diff},
	)


def _create_address(customer_name: str, wave_address: dict) -> str:
	doc = frappe.get_doc(_build_address(customer_name, wave_address))
	doc.insert(ignore_permissions=True)
	return doc.name


def _build_address(customer_name: str, wave_address: dict) -> dict:
	return {
		"doctype": "Address",
		"address_title": f"{customer_name} - {wave_address.get('_id')}",
		"wave_address_id": wave_address.get("_id"),
		"links": [{"link_doctype": "Customer", "link_name": customer_name}],
		**_managed_payload(wave_address),
	}


def _managed_payload(wave_address: dict) -> dict:
	"""Just the fields the integration owns — used for create and diff."""
	return {
		"address_type": _map_type(wave_address.get("type")),
		"address_line1": _line1(wave_address),
		"address_line2": wave_address.get("notice") or "",
		"city": wave_address.get("city") or "",
		"pincode": wave_address.get("postalCode") or "",
		"country": _country(wave_address),
		"phone": wave_address.get("contactPhone") or "",
	}


def _line1(wave_address: dict) -> str:
	parts = [wave_address.get("streetNo"), wave_address.get("street")]
	return " ".join(p for p in parts if p).strip() or "N/A"


def _map_type(wave_type: str | None) -> str:
	return _WAVE_TYPE_TO_ERP.get((wave_type or "").lower(), "Shipping")


def _country(wave_address: dict) -> str:
	explicit = wave_address.get("country")
	if explicit:
		return explicit
	mapped = _TIMEZONE_TO_COUNTRY.get(wave_address.get("timeZone") or "")
	if mapped:
		return mapped
	return _default_country()


def _default_country() -> str:
	company = frappe.db.get_single_value("Wave Settings", "default_company")
	if company:
		return frappe.db.get_value("Company", company, "country") or "Kenya"
	return "Kenya"
