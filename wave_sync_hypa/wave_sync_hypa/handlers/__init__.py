"""Per-entity business handlers.

One module per Wave entity (customer, order, picklist, delivery, invoice,
payment). Each handler's `handle(payload, correlation_id)` function is the
only public entry point and is registered into `services.dispatcher.HANDLER_REGISTRY`
on import. Importing this package triggers every handler module's top-level
code, which populates the registry.
"""

from . import customer  # noqa: F401
