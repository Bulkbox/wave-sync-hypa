"""Master kill switch for the entire Wave integration.

Reads `Wave Settings.enabled` — the master Check field at the top of the
form. Consulted by every inbound dispatch + every outbound worker. Off =>
integration is fully dark: no webhooks dispatched, no HTTP fired at Wave,
no auto-actions taken.
"""

import frappe

STEP_MASTER_DISABLED = "wave_integration_master_disabled"


def is_wave_integration_enabled() -> bool:
	"""Return the current state of the master kill switch."""
	return bool(frappe.get_cached_doc("Wave Settings").get("enabled"))
