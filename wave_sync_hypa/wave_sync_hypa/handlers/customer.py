"""Handle inbound CUSTOMER.UPDATE webhooks.

Orchestration only. Every data decision lives in the resolvers; this module
sequences them and records one Wave Sync Log row per stage boundary.
"""

import frappe

from wave_sync_hypa.wave_sync_hypa.resolvers.address_resolver import append_if_new
from wave_sync_hypa.wave_sync_hypa.resolvers.contact_resolver import upsert_contact
from wave_sync_hypa.wave_sync_hypa.resolvers.customer_resolver import (
	append_business_address_if_present,
	apply_customer_updates,
	find_or_create_customer,
)
from wave_sync_hypa.wave_sync_hypa.services.dispatcher import HANDLER_REGISTRY
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step


def handle(payload: dict, correlation_id: str) -> None:
	"""Orchestrate a CUSTOMER upsert: customer -> updates -> contact -> addresses."""
	wave_id = payload.get("_id")
	wave_updated_at = payload.get("updatedAt")

	customer_name, created, source = find_or_create_customer(payload)
	_log_customer_resolved(correlation_id, payload, customer_name, created, source)
	if source == "email":
		# Adoption is rare and consequential — we just attached a new Wave id to
		# an existing ERP customer. Emit a dedicated Warning row so adoption
		# events are filterable in Wave Sync Log views, not buried in routine Info.
		log_step(
			correlation_id,
			"Customer Adopted by Email",
			"Warning",
			doc_type="CUSTOMER",
			action="UPDATE",
			wave_id=wave_id,
			wave_updated_at=wave_updated_at,
			linked_doctype="Customer",
			linked_docname=customer_name,
			response_body={
				"email": payload.get("email"),
				"adopted_customer": customer_name,
				"new_wave_customer_id": wave_id,
			},
		)

	# Guest payloads route to the shared walk-in Customer. We must never mutate
	# the walk-in record from a per-guest payload — that would overwrite its
	# customer_name, fire Contact changes keyed on the Wave id, and risk tripping
	# Slade's KRA PIN validator if the walk-in carries any tax_id. Skip the
	# identity updates and address append; log the short-circuit for clarity.
	if _is_guest_payload(payload):
		log_step(
			correlation_id,
			"Skipped",
			"Info",
			doc_type="CUSTOMER",
			action="UPDATE",
			wave_id=wave_id,
			wave_updated_at=wave_updated_at,
			linked_doctype="Customer",
			linked_docname=customer_name,
			response_body={"reason": "guest_routed_to_walk_in_customer"},
		)
		return

	apply_customer_updates(customer_name, payload)
	upsert_contact(customer_name, payload)

	for wave_address in payload.get("addresses") or []:
		_append_address(correlation_id, customer_name, wave_address, wave_id, wave_updated_at)

	# B2B-only: businessAddress is a separate top-level field, not part of
	# addresses[]. The resolver handles classification + idempotency; we just
	# log the outcome the same way as a regular Wave address append.
	business_result = append_business_address_if_present(customer_name, payload)
	if business_result is not None:
		address_name, created = business_result
		log_step(
			correlation_id,
			"Resolved Customer",
			"Info",
			doc_type="CUSTOMER",
			action="UPDATE",
			wave_id=wave_id,
			wave_updated_at=wave_updated_at,
			linked_doctype="Address",
			linked_docname=address_name or None,
			response_body={
				"kind": "business_address",
				"created": created,
			},
		)


def _is_guest_payload(payload: dict) -> bool:
	"""Return True when Wave flagged the payload as a guest checkout."""
	return bool(payload.get("isGuest"))


def _log_customer_resolved(
	correlation_id: str, payload: dict, customer_name: str, created: bool, source: str,
) -> None:
	"""Write one Resolved Customer log row recording how the customer was resolved.

	`source` distinguishes guest / primary wave_id / email-adoption / new — useful
	when an unexpected duplicate appears and we need to trace back through Wave
	Sync Log how the Customer was reached.
	"""
	log_step(
		correlation_id,
		"Resolved Customer",
		"Info",
		doc_type="CUSTOMER",
		action="UPDATE",
		wave_id=payload.get("_id"),
		wave_updated_at=payload.get("updatedAt"),
		linked_doctype="Customer",
		linked_docname=customer_name,
		response_body={
			"created": created,
			"is_guest": bool(payload.get("isGuest")),
			"source": source,
		},
	)


def _append_address(
	correlation_id: str,
	customer_name: str,
	wave_address: dict,
	wave_id: str | None,
	wave_updated_at: str | None,
) -> None:
	"""Append one Wave address if it's new; log whether an Address was created or reused."""
	address_name, created = append_if_new(customer_name, wave_address)
	log_step(
		correlation_id,
		"Resolved Customer",
		"Info",
		doc_type="CUSTOMER",
		action="UPDATE",
		wave_id=wave_id,
		wave_updated_at=wave_updated_at,
		linked_doctype="Address",
		linked_docname=address_name or None,
		response_body={
			"wave_address_id": wave_address.get("_id"),
			"created": created,
		},
	)


# Register this handler into the dispatcher registry on module import.
HANDLER_REGISTRY["customer_upsert"] = handle
