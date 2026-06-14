"""Map a Wave customer payload to an ERP Customer.

Guest payloads are routed to the single Walk-in Customer configured in
Wave Settings. Non-guest payloads are found by `wave_customer_id` (the
stable Wave `_id`) or created fresh. Updates apply only to mutable
identity fields — never to inventory, credit terms, or any field we do
not own.

B2B vs B2C
----------
Wave's payload carries `customerType` ("b2b" | "b2c"), `companyName`,
`businessType`, `businessAddress`, `city`, and `fiscalId` (renamed to
`taxId` in a future Wave release — we read both). When `customerType ==
"b2b"` we map onto ERPNext's native customer record:

  customer_type  = "Company"   (Individual otherwise)
  customer_name  = companyName (firstName + lastName otherwise)
  customer_group = lookup Customer Group whose name == businessType,
                   falling back to Wave Settings.default_customer_group
                   and writing a Frappe Error Log row.
  tax_id         = fiscalId or taxId, whichever Wave sent.

We deliberately store NONE of the Wave-side fields on the Customer
record beyond what ERPNext already exposes — Wave is the source of
truth for the raw data; ERPNext only carries the derived classification
that downstream accounting (selling rules, GL grouping) actually uses.

Business address
----------------
For B2B customers Wave also sends `businessAddress` + `city`, separate
from the `addresses[]` array used for delivery. We synthesise a
Wave-shaped address dict from those two fields and feed it through the
existing `append_if_new` so re-sends are idempotent (deterministic
synthetic `_id = f"business:{wave_customer_id}"`). The resulting
Address is labelled "Business Address" and typed `Office`.
"""

import frappe

from wave_sync_hypa.wave_sync_hypa.resolvers.address_resolver import append_if_new
from wave_sync_hypa.wave_sync_hypa.resolvers.contact_resolver import upsert_contact
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveResolutionError


def find_customer_by_wave_id(wave_customer_id: str | None) -> str | None:
	"""Return the ERP Customer name whose wave_customer_id matches, or None."""
	if not wave_customer_id:
		return None
	return frappe.db.get_value("Customer", {"wave_customer_id": wave_customer_id}, "name")


def find_customer_by_email(email: str | None, wave_customer_id: str | None) -> str | None:
	"""Secondary lookup: find a Customer whose linked Contact carries this email.

	Used as a fallback when the primary `wave_customer_id` lookup misses, so we
	adopt an existing ERP record instead of creating a duplicate when Wave
	emits a new `_id` for someone we already know (account reset, manual ERP
	entry, etc.). Returns the Customer name only when adoption is safe; logs
	and returns None when:

	  * email is empty or whitespace-only,
	  * the email matches multiple ERP customers (ambiguous — never pick one),
	  * the single match is already linked to a different wave_customer_id
	    (two distinct Wave accounts share an email; merging here would be
	    lossy and silently re-assign delivery history to the wrong account).

	Case-insensitive comparison via LOWER() so the match works regardless of
	MySQL collation. The join walks Contact Email -> Dynamic Link -> Customer
	because ERPNext Customer doesn't carry a top-level email field; email
	lives on the Contact, which is linked back to the Customer through a
	Dynamic Link row.
	"""
	email = (email or "").strip()
	if not email:
		return None

	rows = frappe.db.sql(
		"""
		SELECT DISTINCT dl.link_name AS customer_name
		FROM `tabContact Email` ce
		JOIN `tabDynamic Link` dl
		  ON dl.parent = ce.parent
		 AND dl.parenttype = 'Contact'
		WHERE LOWER(ce.email_id) = LOWER(%s)
		  AND dl.link_doctype = 'Customer'
		""",
		(email,),
		as_dict=True,
	)
	if not rows:
		return None

	# Strict: any time more than one ERP customer shares this email, the email
	# is no longer a trustworthy dedup signal — even if only one of them is
	# wave-linkage-safe. We decline and let the operator clean up the
	# duplicates manually. The new Wave customer lands as a fresh record.
	if len(rows) > 1:
		frappe.log_error(
			title="wave_sync_hypa: multiple ERP customers share an email",
			message=(
				f"Email '{email}' matches multiple ERP customers: "
				f"{[r['customer_name'] for r in rows]}. Skipping email-based dedup so "
				"we don't attach the new Wave id to the wrong record; a new ERP "
				"Customer will be created. Merge the existing duplicates manually."
			),
		)
		return None

	candidate = rows[0]["customer_name"]
	existing_wave_id = (
		frappe.db.get_value("Customer", candidate, "wave_customer_id") or ""
	).strip()
	if existing_wave_id and existing_wave_id != (wave_customer_id or ""):
		# The single ERP candidate is already linked to a different Wave account.
		# Two distinct Wave accounts share an email — never silently merge them.
		frappe.log_error(
			title="wave_sync_hypa: email match has conflicting wave_customer_id",
			message=(
				f"Email '{email}' matches existing ERP customer '{candidate}' but it "
				f"is already linked to Wave id '{existing_wave_id}'. New Wave id "
				f"'{wave_customer_id}' will create a separate ERP Customer; merge "
				"manually if these are the same human."
			),
		)
		return None

	return candidate


def _stamp_wave_customer_id(customer_name: str, wave_customer_id: str | None) -> None:
	"""Attach a Wave id to an existing Customer adopted via email match.

	Uses `update_modified=False` so the doc's modified timestamp doesn't tick
	(the subsequent apply_customer_updates already touches it). Skips silently
	when wave_customer_id is empty — adoption without an id would be a no-op.
	"""
	if not wave_customer_id:
		return
	frappe.db.set_value(
		"Customer",
		customer_name,
		"wave_customer_id",
		wave_customer_id,
		update_modified=False,
	)


def find_or_create_customer(payload: dict) -> tuple[str, bool, str]:
	"""Return (customer_name, created_flag, source).

	`source` is one of:
	  - "guest"   : routed to the configured walk-in Customer; no identity write
	  - "wave_id" : found by primary `wave_customer_id` lookup
	  - "email"   : adopted an existing ERP Customer by email match; the new
	                wave_customer_id has been stamped onto that record so the
	                next call short-circuits on the primary lookup
	  - "new"     : freshly inserted

	The email branch is opt-in via Wave Settings.customer_email_fallback_enabled
	(default off). When the setting is off, the waterfall is just wave_id ->
	create — the original behaviour. When on, we adopt existing ERP customers
	(manual entries, prior imports, Wave accounts re-registered under a new
	`_id`). Safety branches in find_customer_by_email mean we never silently
	merge ambiguous matches even when the setting is on.
	"""
	if _is_guest(payload):
		return _get_walk_in_customer_name(), False, "guest"

	wave_customer_id = payload.get("_id")
	existing = find_customer_by_wave_id(wave_customer_id)
	if existing:
		return existing, False, "wave_id"

	if _email_fallback_enabled():
		by_email = find_customer_by_email(payload.get("email"), wave_customer_id)
		if by_email:
			_stamp_wave_customer_id(by_email, wave_customer_id)
			return by_email, False, "email"

	return _create_customer_from_wave(payload), True, "new"


def _email_fallback_enabled() -> bool:
	"""Read the Wave Settings switch that gates the email-based secondary lookup."""
	return bool(
		frappe.db.get_single_value("Wave Settings", "customer_email_fallback_enabled")
	)


def apply_customer_updates(customer_name: str, payload: dict) -> None:
	"""Update mutable identity fields on an existing Customer; leave everything else untouched.

	For B2B payloads we also promote the derived classification (customer_type,
	customer_group, tax_id) onto the existing record so a customer that started
	as B2C and later upgraded to B2B gets correctly re-classified. Walk-in /
	individual / non-Wave fields untouched.
	"""
	doc = frappe.get_doc("Customer", customer_name)
	doc.customer_name = _resolve_customer_name(payload) or doc.customer_name
	doc.wave_integrator_id = payload.get("integratorId") or doc.wave_integrator_id
	doc.is_wave_customer = 1

	# Derived classification — only overwrite when Wave has something specific
	# to say. A b2c payload that omits these leaves the existing values alone.
	customer_type = _resolve_customer_type(payload)
	if customer_type:
		doc.customer_type = customer_type
	customer_group = _resolve_customer_group(payload)
	if customer_group:
		doc.customer_group = customer_group
	tax_id = _resolve_tax_id(payload)
	if tax_id:
		doc.tax_id = tax_id

	# Same mandatory-bypass rationale as create: Wave does not carry a KRA PIN.
	doc.flags.ignore_mandatory = True
	doc.save(ignore_permissions=True)


def append_business_address_if_present(customer_name: str, payload: dict) -> tuple[str, bool] | None:
	"""For B2B customers with a businessAddress, ensure an Office Address exists.

	Synthesises a Wave-shaped address dict from the payload's `businessAddress`
	+ `city`, then routes through the existing `append_if_new` so re-sending
	the same CUSTOMER.UPDATE does not create duplicate Address rows. The
	synthetic wave_address_id is deterministic: `f"business:{_id}"`.

	Returns (address_name, created) on success, None when nothing was done
	(no businessAddress, no wave_customer_id, or b2c payload). The address
	is labelled "Business Address" and typed Office in ERPNext semantics.
	"""
	customer_type = _resolve_customer_type(payload)
	if customer_type != "Company":
		return None
	business_address = (payload.get("businessAddress") or "").strip()
	wave_customer_id = payload.get("_id")
	if not business_address or not wave_customer_id:
		return None

	city = (payload.get("city") or "").strip() or None
	synth_address = {
		"_id": f"business:{wave_customer_id}",
		# "headquarters" maps to ERPNext "Office" via the existing address_resolver
		# _WAVE_TYPE_TO_ERP table — keeps the type-mapping logic in one place.
		"type": "headquarters",
		"street": business_address,
		"city": city,
	}
	address_name, created = append_if_new(customer_name, synth_address)
	if created and address_name:
		# Rename the auto-generated title so the Address list view shows
		# something operator-readable; the resolver's default title format is
		# `"<customer> - <wave_address_id>"` which would be unfriendly here.
		frappe.db.set_value(
			"Address",
			address_name,
			"address_title",
			f"{customer_name} - Business Address",
			update_modified=False,
		)
	return address_name, created


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

	B2B payloads land here too. `_resolve_*` helpers below branch on
	`customerType`; for b2c they return the same values the old code did so
	the existing flow is preserved.

	On creation we also seed the credit limit (from Wave Settings, default
	company) and create the primary Contact — both happen ONLY here, so an
	existing customer's credit limit and contact are never overwritten on a
	later webhook/order.
	"""
	doc = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": _resolve_customer_name(payload) or payload.get("email") or payload.get("_id"),
			"customer_type": _resolve_customer_type(payload) or "Individual",
			"customer_group": _resolve_customer_group(payload) or _first_customer_group(),
			"territory": _default("default_territory") or _first_territory(),
			"tax_id": _resolve_tax_id(payload),
			"wave_customer_id": payload.get("_id"),
			"wave_integrator_id": payload.get("integratorId"),
			"is_wave_customer": 1,
			"require_tax_id": 0,
			"credit_limits": _initial_credit_limits(),
		}
	)
	# Wave customers arrive without KRA PINs. A site-level Property Setter from
	# kenya_compliance_via_slade makes `tax_id` mandatory; since we already
	# disabled `require_tax_id` on the record, we also bypass the framework's
	# mandatory check so the insert can land. Accounting can add the PIN later.
	doc.flags.ignore_mandatory = True
	doc.insert(ignore_permissions=True)
	# Mirror the address flow: create + link the primary Contact from whatever
	# identity Wave gave us (name / email / phone). Same payload shape as the
	# CUSTOMER webhook, so order-originated customers get a Contact too.
	upsert_contact(doc.name, payload)
	return doc.name


def _initial_credit_limits() -> list[dict]:
	"""Return a one-row credit-limit table for the default company, from Wave Settings.

	Empty (no row) when either the amount or the Default Company is unset — we
	never fabricate a credit limit without a company to scope it to.
	"""
	amount = _default("default_customer_credit_limit")
	company = _default("default_company")
	if not amount or not company:
		return []
	return [{"company": company, "credit_limit": amount}]


def _resolve_customer_type(payload: dict) -> str | None:
	"""Map Wave's `customerType` to ERPNext's `customer_type`. b2b -> Company, else Individual.

	Returns None when Wave did not send `customerType` at all (legacy payloads),
	so apply_customer_updates can leave the existing field alone.
	"""
	wave_type = (payload.get("customerType") or "").strip().lower()
	if not wave_type:
		return None
	return "Company" if wave_type == "b2b" else "Individual"


def _resolve_customer_name(payload: dict) -> str:
	"""For B2B payloads prefer Wave's companyName; for B2C fall back to first+last name."""
	if (payload.get("customerType") or "").strip().lower() == "b2b":
		company = (payload.get("companyName") or "").strip()
		if company:
			return company
	return _full_name(payload)


def _resolve_tax_id(payload: dict) -> str | None:
	"""Read fiscalId (current) or taxId (future) and return whichever Wave sent."""
	for key in ("fiscalId", "taxId"):
		value = (payload.get(key) or "").strip()
		if value:
			return value
	return None


def _resolve_customer_group(payload: dict) -> str | None:
	"""For B2B payloads map businessType to a Customer Group via the configurable
	Wave Settings.business_type_mappings table; else use the default.

	When `businessType` is set but no mapping row matches, fall back to
	Wave Settings.default_customer_group AND write a Frappe Error Log row so
	accounting knows to add the missing mapping. Returns None only when both
	the mapping and the default fail (let _create / _first_customer_group fill
	in the absolute fallback).
	"""
	customer_type = (payload.get("customerType") or "").strip().lower()
	business_type = (payload.get("businessType") or "").strip()
	if customer_type == "b2b" and business_type:
		group = _match_business_type(business_type)
		if group:
			return group
		# No mapping matched on this site. Log to the Frappe Error Log so it
		# surfaces in the desk's standard triage view; fall back to the default.
		frappe.log_error(
			title="wave_sync_hypa: no Customer Group mapping for businessType",
			message=(
				f"Wave customer payload has businessType='{business_type}' but no "
				f"Wave Settings Business Type Mapping row matches it. Falling back to "
				f"Wave Settings.default_customer_group. Add a mapping row to classify "
				f"this and future customers correctly."
			),
		)
	return _default("default_customer_group")


def _match_business_type(business_type: str) -> str | None:
	"""Return the mapped Customer Group for a Wave businessType, or None.

	A mapping row matches when its key and the incoming businessType contain one
	another (case-insensitive), so a row keyed 'restaurant' matches Wave's
	'restaurant/cafe/hotel'. The longest matching key wins (closest match).
	"""
	incoming = business_type.lower()
	rows = frappe.get_cached_doc("Wave Settings").get("business_type_mappings") or []
	matches = []
	for row in rows:
		key = (row.get("wave_business_type") or "").strip().lower()
		if key and (key in incoming or incoming in key):
			matches.append(row)
	if not matches:
		return None
	best = max(matches, key=lambda r: len(r.get("wave_business_type") or ""))
	return best.get("erp_customer_group")


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
