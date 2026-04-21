"""Per-entity business handlers.

One module per Wave entity (customer, order, picklist, delivery, invoice,
payment). Each handler's `handle(payload, correlation_id)` function is the
only public entry point and is registered in `services.dispatcher`.
"""
