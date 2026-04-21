"""Upsert the ERPNext Contact linked to a Wave customer.

Wave sends all contact info inside the Customer payload (no separate contact
_id), so we key the Contact by the customer's `wave_contact_id` custom field
set to the Wave customer `_id`. One Contact per Wave customer.
"""

import frappe


def upsert_contact(customer_name: str, payload: dict) -> str | None:
	"""Create or update the primary Contact for this Customer; return its name."""
	wave_customer_id = payload.get("_id")
	if not wave_customer_id:
		return None
	existing = _find_contact(wave_customer_id)
	if existing:
		_apply_updates(existing, payload)
		return existing
	return _create_contact(customer_name, payload)


def _find_contact(wave_customer_id: str) -> str | None:
	"""Return the Contact whose wave_contact_id matches, or None."""
	return frappe.db.get_value("Contact", {"wave_contact_id": wave_customer_id}, "name")


def _create_contact(customer_name: str, payload: dict) -> str:
	"""Insert a new Contact, link it to the Customer, and fill identity + primary email/phone."""
	doc = frappe.get_doc(
		{
			"doctype": "Contact",
			"first_name": payload.get("firstName") or payload.get("email") or "Wave Contact",
			"last_name": payload.get("lastName"),
			"wave_contact_id": payload.get("_id"),
			"links": [{"link_doctype": "Customer", "link_name": customer_name}],
			"email_ids": _email_ids(payload),
			"phone_nos": _phone_nos(payload),
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _apply_updates(contact_name: str, payload: dict) -> None:
	"""Refresh mutable identity fields and primary email/phone on an existing Contact."""
	doc = frappe.get_doc("Contact", contact_name)
	doc.first_name = payload.get("firstName") or doc.first_name
	doc.last_name = payload.get("lastName") or doc.last_name
	_replace_emails(doc, _email_ids(payload))
	_replace_phones(doc, _phone_nos(payload))
	doc.save(ignore_permissions=True)


def _email_ids(payload: dict) -> list[dict]:
	"""Return the Contact child-table rows for email_ids (exactly one primary entry if email present)."""
	email = payload.get("email")
	if not email:
		return []
	return [{"email_id": email, "is_primary": 1}]


def _phone_nos(payload: dict) -> list[dict]:
	"""Return the Contact child-table rows for phone_nos (exactly one primary mobile if phone present)."""
	phone = payload.get("mobilePhone")
	if not phone:
		return []
	return [{"phone": phone, "is_primary_mobile_no": 1, "is_primary_phone": 1}]


def _replace_emails(doc, new_rows: list[dict]) -> None:
	"""Overwrite the Contact's email_ids child table with the new rows."""
	doc.email_ids = []
	for row in new_rows:
		doc.append("email_ids", row)


def _replace_phones(doc, new_rows: list[dict]) -> None:
	"""Overwrite the Contact's phone_nos child table with the new rows."""
	doc.phone_nos = []
	for row in new_rows:
		doc.append("phone_nos", row)
