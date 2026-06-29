"""In-process bridge to the same-site iPay app's payment lookup.

`ipay` is a Frappe app installed on this bench, so we call
`ipay.api.get_transaction` directly as a Python function rather than over
HTTP — no API tokens, no network hop to our own site. That function returns
`{"oid", "paid", "data"}` directly (the `"message"` wrapper is added only by
Frappe's HTTP layer) and `frappe.throw`s on a missing oid, unconfigured iPay
Settings, or an iPay network error.

This module is the single place that knows iPay can be absent or unhappy: it
NEVER raises. Callers get a structured envelope and degrade on it — a missing
app or a flaky iPay call must never break Sales Order / Sales Invoice flow.
"""

from __future__ import annotations

import frappe

IPAY_APP = "ipay"


def is_ipay_available() -> bool:
	"""True when the iPay app is installed on this site."""
	return IPAY_APP in frappe.get_installed_apps()


def fetch_transaction(oid: str) -> dict:
	"""Look an iPay payment up by oid; never raise.

	Returns a structured envelope:
	    {"available": bool, "paid": bool, "data": dict | None, "error": str | None}

	  * available=False           -> iPay app is not installed on this site.
	  * paid=True, data={...}      -> iPay confirms a completed payment.
	  * paid=False, data=None      -> no completed payment for this oid, OR the
	                                  lookup could not be made (see `error`:
	                                  iPay unconfigured / unreachable / other).
	"""
	oid = (oid or "").strip()
	if not oid:
		return {"available": True, "paid": False, "data": None, "error": "oid is empty"}
	if not is_ipay_available():
		return {"available": False, "paid": False, "data": None, "error": "iPay app is not installed"}
	try:
		from ipay.api import get_transaction
	except ImportError:
		# The iPay app is installed but predates its get_transaction API
		# (ipay.api, added 2026-06-02). Surface an actionable message instead of
		# the raw "No module named 'ipay.api'" so ops know to update the iPay app.
		return {
			"available": True,
			"paid": False,
			"data": None,
			"error": "iPay app is installed but missing the get_transaction API (ipay.api); update the iPay app on this site.",
		}

	try:
		result = get_transaction(oid) or {}
		return {
			"available": True,
			"paid": bool(result.get("paid")),
			"data": result.get("data"),
			"error": None,
		}
	except Exception as exc:
		# frappe.throw (unconfigured iPay Settings / iPay unreachable) raises
		# frappe.ValidationError; any other failure is caught here too. We
		# degrade to "could not verify" rather than propagating.
		return {"available": True, "paid": False, "data": None, "error": str(exc)}
