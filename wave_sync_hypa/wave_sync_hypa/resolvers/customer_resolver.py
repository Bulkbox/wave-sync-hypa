"""Map a Wave customer payload to an ERP Customer.

Guest payloads are routed to the single Walk-in Customer configured in
Wave Settings. Non-guest payloads are found by `wave_customer_id` (the
stable Wave `_id`) or created fresh. Updates apply only to mutable
identity fields — never to inventory, credit terms, or any field we do
not own.
"""

import frappe

from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveResolutionError


def find_customer_by_wave_id(wave_customer_id: str | None) -> str | None:
	"""Return the ERP Customer name whose wave_customer_id matches, or None."""
	if not wave_customer_id:
		return None
	return frappe.db.get_value("Customer", {"wave_customer_id": wave_customer_id}, "name")


def find_or_create_customer(payload: dict) -> tuple[str, bool]:
	"""Return (customer_name, created_flag). Guests resolve to the walk-in customer."""
	if _is_guest(payload):
		return _get_walk_in_customer_name(), False

	wave_customer_id = payload.get("_id")
	existing = find_customer_by_wave_id(wave_customer_id)
	if existing:
		return existing, False

	return _create_customer_from_wave(payload), True


def apply_customer_updates(customer_name: str, payload: dict) -> None:
	"""Update mutable identity fields on an existing Customer; leave everything else untouched."""
	doc = frappe.get_doc("Customer", customer_name)
	doc.customer_name = _full_name(payload) or doc.customer_name
	doc.wave_integrator_id = payload.get("integratorId") or doc.wave_integrator_id
	doc.is_wave_customer = 1
	# Same mandatory-bypass rationale as create: Wave does not carry a KRA PIN.
	doc.flags.ignore_mandatory = True
	doc.save(ignore_permissions=True)


def _is_guest(payload: dict) -> bool:
	"""Return True when Wave flags the customer as a guest checkout."""
	return bool(payload.get("isGuest"))


def _get_walk_in_customer_name() -> str:
	"""Return the Customer configured as walk-in in Wave Settings; raise if missing."""
	name = frappe.db.get_single_value("Wave Settings", "walk_in_customer")
	if not name:
		raise WaveResolutionError(
			"Wave Settings.walk_in_customer is not configured; guest orders cannot be processed."
		)
	return name


def _create_customer_from_wave(payload: dict) -> str:
	"""Insert a new Customer keyed by wave_customer_id and return its name.

	Wave does not carry KRA PINs, so `require_tax_id` is explicitly cleared.
	Accounting can flip it back on for individual customers later if a PIN
	becomes required for their invoices; leaving the Slade default in place
	would block the storefront entirely on a fresh site.
	"""
	doc = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": _full_name(payload) or payload.get("email") or payload.get("_id"),
			"customer_type": "Individual",
			"customer_group": _default("default_customer_group") or _first_customer_group(),
			"territory": _default("default_territory") or _first_territory(),
			"wave_customer_id": payload.get("_id"),
			"wave_integrator_id": payload.get("integratorId"),
			"is_wave_customer": 1,
			"require_tax_id": 0,
		}
	)
	# Wave customers arrive without KRA PINs. A site-level Property Setter from
	# kenya_compliance_via_slade makes `tax_id` mandatory; since we already
	# disabled `require_tax_id` on the record, we also bypass the framework's
	# mandatory check so the insert can land. Accounting can add the PIN later.
	doc.flags.ignore_mandatory = True
	doc.insert(ignore_permissions=True)
	return doc.name


def _full_name(payload: dict) -> str:
	"""Return "First Last" or whichever half is present, trimmed."""
	parts = [payload.get("firstName"), payload.get("lastName")]
	return " ".join(p for p in parts if p).strip()


def _default(fieldname: str) -> str | None:
	"""Return a configured default value from Wave Settings."""
	return frappe.db.get_single_value("Wave Settings", fieldname)


def _first_customer_group() -> str:
	"""Fallback: return the first non-group Customer Group so Customer insert doesn't fail."""
	return frappe.db.get_value("Customer Group", {"is_group": 0}, "name") or "All Customer Groups"


def _first_territory() -> str:
	"""Fallback: return the first non-group Territory so Customer insert doesn't fail."""
	return frappe.db.get_value("Territory", {"is_group": 0}, "name") or "All Territories"
