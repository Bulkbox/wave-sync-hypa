"""Handle inbound CUSTOMER.UPDATE webhooks.

Orchestration only. Every data decision lives in the resolvers; this module
sequences them and records one Wave Sync Log row per stage boundary.
"""

import frappe

from wave_sync_hypa.wave_sync_hypa.resolvers.address_resolver import append_if_new
from wave_sync_hypa.wave_sync_hypa.resolvers.contact_resolver import upsert_contact
from wave_sync_hypa.wave_sync_hypa.resolvers.customer_resolver import (
	apply_customer_updates,
	find_or_create_customer,
)
from wave_sync_hypa.wave_sync_hypa.services.dispatcher import HANDLER_REGISTRY
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step


def handle(payload: dict, correlation_id: str) -> None:
	"""Orchestrate a CUSTOMER upsert: customer -> updates -> contact -> addresses."""
	wave_id = payload.get("_id")
	wave_updated_at = payload.get("updatedAt")

	customer_name, created = find_or_create_customer(payload)
	_log_customer_resolved(correlation_id, payload, customer_name, created)

	apply_customer_updates(customer_name, payload)
	upsert_contact(customer_name, payload)

	for wave_address in payload.get("addresses") or []:
		_append_address(correlation_id, customer_name, wave_address, wave_id, wave_updated_at)


def _log_customer_resolved(
	correlation_id: str, payload: dict, customer_name: str, created: bool
) -> None:
	"""Write one Resolved Customer log row recording whether the customer was new or existing."""
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
		response_body={"created": created, "is_guest": bool(payload.get("isGuest"))},
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
