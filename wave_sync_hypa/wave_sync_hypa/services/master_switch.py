"""Master kill switch for the entire Wave integration.

Reads `Wave Settings.enabled` — the master Check field at the top of the
form. Consulted at two altitudes:

  * the decision layer (ERP-side doc_event handlers + the dispatch fan-out),
    via `skip_if_disabled`, so that with the switch off NO background job is
    even enqueued; and
  * every outbound worker, via `is_wave_integration_enabled`, as a backstop
    for direct / replayed / console invocations that bypass the handlers.

Off => integration is fully dark: no webhooks dispatched, no jobs queued, no
HTTP fired at Wave, no auto-actions taken.
"""

import frappe

from wave_sync_hypa.wave_sync_hypa.services.logger import log_step

STEP_MASTER_DISABLED = "wave_integration_master_disabled"


def is_wave_integration_enabled() -> bool:
	"""Return the current state of the master kill switch."""
	return bool(frappe.get_cached_doc("Wave Settings").get("enabled"))


def skip_if_disabled(correlation_id: str, **log_fields) -> bool:
	"""Decision-layer guard: log + signal "skip" when the master switch is OFF.

	Call this at the point where outbound work is *decided* — the ERP-side
	doc_event handlers and the dispatch fan-out — so that with the integration
	disabled no job is queued and no further log noise is produced. "Off" means
	do nothing outward, not "queue work the worker will silently drop".

	Returns True (after writing one STEP_MASTER_DISABLED audit row) when the
	switch is off; False when it is on. `log_fields` are forwarded verbatim to
	`log_step` (doc_type, action, linked_doctype, linked_docname, wave_id, ...).

	The outbound workers keep their own `is_wave_integration_enabled()` check as
	a backstop for callers that don't pass through a decision-layer site.
	"""
	if is_wave_integration_enabled():
		return False
	log_step(correlation_id, STEP_MASTER_DISABLED, "Info", **log_fields)
	return True
