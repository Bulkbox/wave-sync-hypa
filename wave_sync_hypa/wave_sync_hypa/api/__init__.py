"""HTTP surface: whitelisted endpoints for inbound Wave webhooks.

No business logic lives here. Modules in this package only authenticate,
validate transport-level shape, log receipt, and delegate to `services`.
"""
