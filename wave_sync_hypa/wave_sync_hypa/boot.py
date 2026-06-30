"""Surface the feature gates the desk client scripts need.

Wave Settings is a permission-restricted Single, so a Sales / Accounts user
viewing a Sales Invoice can't read it from the browser (a client-side
get_single_value would reject on permissions). We expose the two booleans the
Sales Invoice client script needs into bootinfo — computed server-side for every
desk session — so the script can gate synchronously, with no permission-gated
round-trip and no race against iPay's boot-loaded buttons.
"""

from __future__ import annotations

import frappe


def boot_session(bootinfo) -> None:
	"""extend_bootinfo hook: publish the prepaid-PE gates to the desk."""
	try:
		settings = frappe.get_cached_doc("Wave Settings")
	except Exception:
		return
	bootinfo.wave_integration_enabled = 1 if settings.get("enabled") else 0
	bootinfo.wave_ipay_auto_create_payment_entry = 1 if settings.get("ipay_auto_create_payment_entry") else 0
