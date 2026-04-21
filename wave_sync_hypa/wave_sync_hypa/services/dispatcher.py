"""Route inbound webhook payloads to handlers via the Wave Route Rule table.

The handler registry is a fixed dict in code — admins cannot invent new
handlers at runtime, only enable or disable mappings. Adding a new handler
is a Python change plus a registry entry here.

A handler is a callable with the signature::

	def handle(payload: dict, correlation_id: str) -> None

It is called exactly once per matching webhook after idempotency and
dispatch have been resolved. Handlers are responsible for their own
per-step logging; the dispatcher only logs dispatch-level events.
"""

from collections.abc import Callable

import frappe

HANDLER_REGISTRY: dict[str, Callable[[dict, str], None] | None] = {
	"customer_upsert": None,
	"order_create": None,
	"order_update": None,
	"order_cancel": None,
	"picklist_apply": None,
	"delivery_create": None,
	"invoice_create": None,
	"payment_apply": None,
}


def resolve_handler(doc_type: str, action: str) -> Callable[[dict, str], None] | None:
	"""Return the handler callable for (doc_type, action) or None if no enabled rule matches."""
	handler_key = _lookup_handler_key(doc_type, action)
	if handler_key is None:
		return None
	return HANDLER_REGISTRY.get(handler_key)


def _lookup_handler_key(doc_type: str, action: str) -> str | None:
	"""Return the first enabled handler_key for (doc_type, action) from Wave Settings."""
	settings = frappe.get_cached_doc("Wave Settings")
	for rule in settings.get("route_rules") or []:
		if rule.doc_type == doc_type and rule.action == action and rule.enabled:
			return rule.handler_key
	return None
