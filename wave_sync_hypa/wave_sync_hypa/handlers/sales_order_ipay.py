"""Sales Order hook: verify a prepaid order's iPay payment on creation.

Wired in hooks.py:

  Sales Order.after_insert -> fetch_ipay_on_prepaid_insert

Fires for every Sales Order insert but is a tight no-op unless the order is a
prepaid Wave order (wave_payment_classification == "prepaid") carrying a Wave
friendly id (the iPay oid). For those, it enqueues an async iPay lookup
(after_commit, so the SO is persisted before the worker runs) that stamps the
wave_ipay_* fields and flags the SO for accounting if the payment can't be
verified. Gated by the iPay verification flag and the master kill switch.
"""

from __future__ import annotations

import frappe

from wave_sync_hypa.wave_sync_hypa.services import ipay_payment_sync
from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id
from wave_sync_hypa.wave_sync_hypa.services.master_switch import skip_if_disabled


def fetch_ipay_on_prepaid_insert(doc, method=None) -> None:
	"""after_insert: enqueue an iPay payment fetch for a freshly-created prepaid Wave SO."""
	if (doc.get("wave_payment_classification") or "") != "prepaid":
		return
	if not (doc.get("wave_friendly_id") or "").strip():
		return
	settings = frappe.get_cached_doc("Wave Settings")
	if not settings.get("ipay_verification_enabled"):
		return
	correlation_id = doc.get("wave_correlation_id") or new_correlation_id()
	if skip_if_disabled(
		correlation_id,
		doc_type="Sales Order",
		action="ipay_payment_fetch",
		linked_doctype="Sales Order",
		linked_docname=doc.name,
	):
		return
	ipay_payment_sync.enqueue_fetch(doc.name, correlation_id)
