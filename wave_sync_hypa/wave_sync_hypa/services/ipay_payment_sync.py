"""Verify a prepaid Sales Order's payment against iPay and stamp the details.

`fetch_and_stamp` is the shared core used by both entry points:
  * the operator "Verify iPay Payment" button (synchronous, via api.sales_order); and
  * the automatic fetch enqueued when a prepaid SO is created (async worker).

It looks the payment up by the SO's `wave_friendly_id` (which is the iPay
oid), stamps the `wave_ipay_*` fields, and either clears the accounting
review flag (payment confirmed) or sets it (payment could not be verified —
iPay absent, not-paid, or unreachable). All field writes use db.set_value so
they work whether the SO is draft or submitted and don't re-run validate.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt, get_datetime, now_datetime

from wave_sync_hypa.wave_sync_hypa.services import ipay_gateway, payment_review_flag
from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step
from wave_sync_hypa.wave_sync_hypa.services.master_switch import is_wave_integration_enabled

WORKER_DOTTED_PATH = (
	"wave_sync_hypa.wave_sync_hypa.services.ipay_payment_sync.fetch_and_stamp_worker"
)

STEP_ENQUEUED = "ipay_payment_fetch_enqueued"
STEP_ATTEMPT = "ipay_payment_fetch_attempt"
STEP_PAID = "ipay_payment_fetch_paid"
STEP_UNVERIFIED = "ipay_payment_fetch_unverified"
STEP_SKIPPED_NOT_PREPAID = "ipay_payment_fetch_skipped_not_prepaid"
STEP_UNEXPECTED_ERROR = "ipay_payment_fetch_unexpected_error"


def enqueue_fetch(sales_order_name: str, correlation_id: str) -> None:
	"""Queue the async iPay fetch for a prepaid SO (after_commit so the SO exists first)."""
	frappe.enqueue(
		WORKER_DOTTED_PATH,
		queue="default",
		enqueue_after_commit=True,
		job_name=f"ipay_fetch:{sales_order_name}",
		sales_order_name=sales_order_name,
		correlation_id=correlation_id,
	)
	log_step(
		correlation_id=correlation_id,
		step=STEP_ENQUEUED,
		level="Info",
		doc_type="Sales Order",
		linked_doctype="Sales Order",
		linked_docname=sales_order_name,
	)


def fetch_and_stamp_worker(*, sales_order_name: str, correlation_id: str) -> None:
	"""Async entry: delegate to fetch_and_stamp (which enforces the gates). Never raises."""
	try:
		fetch_and_stamp(sales_order_name, correlation_id)
	except Exception as exc:
		log_step(
			correlation_id=correlation_id,
			step=STEP_UNEXPECTED_ERROR,
			level="Error",
			doc_type="Sales Order",
			linked_doctype="Sales Order",
			linked_docname=sales_order_name,
			error_message=f"unexpected exception in fetch_and_stamp_worker: {exc}",
			stack_trace=frappe.get_traceback(),
		)


def fetch_and_stamp(sales_order_name: str, correlation_id: str = "", *, settings=None) -> dict:
	"""Look iPay up for a prepaid SO, stamp wave_ipay_* fields, flag/clear review.

	The single enforcement point for both the master kill switch and the
	`ipay_verification_enabled` flag — so the operator button, the async
	worker, and any direct caller all honour them identically. When a gate is
	off we return without flagging the SO (the feature is dark, not failing).

	Returns a structured result for the button to render:
	    {"ok": bool, "paid": bool, "data": dict | None, "reason": str | None}
	`ok` is True when the lookup ran (paid or not); False for a closed gate or
	a guard failure (SO missing / not prepaid / no oid).
	"""
	correlation_id = correlation_id or new_correlation_id()
	if not is_wave_integration_enabled():
		return {"ok": False, "paid": False, "data": None, "reason": "Wave integration is disabled."}
	settings = settings or frappe.get_cached_doc("Wave Settings")
	if not settings.get("ipay_verification_enabled"):
		return {
			"ok": False, "paid": False, "data": None,
			"reason": "iPay verification is disabled in Wave Settings.",
		}
	so = frappe.db.get_value(
		"Sales Order",
		sales_order_name,
		["name", "wave_payment_classification", "wave_friendly_id"],
		as_dict=True,
	)
	if not so:
		return {"ok": False, "paid": False, "data": None, "reason": "Sales Order not found"}
	if (so.wave_payment_classification or "") != "prepaid":
		log_step(
			correlation_id=correlation_id,
			step=STEP_SKIPPED_NOT_PREPAID,
			level="Info",
			doc_type="Sales Order",
			linked_doctype="Sales Order",
			linked_docname=sales_order_name,
		)
		return {"ok": False, "paid": False, "data": None, "reason": "not a prepaid order"}

	oid = (so.wave_friendly_id or "").strip()
	if not oid:
		reason = "Sales Order has no Wave friendly id (iPay oid); cannot verify payment."
		payment_review_flag.flag(
			"Sales Order", sales_order_name, reason, settings=settings, correlation_id=correlation_id,
		)
		return {"ok": False, "paid": False, "data": None, "reason": reason}

	log_step(
		correlation_id=correlation_id,
		step=STEP_ATTEMPT,
		level="Info",
		doc_type="Sales Order",
		linked_doctype="Sales Order",
		linked_docname=sales_order_name,
		request_body={"oid": oid},
	)
	result = ipay_gateway.fetch_transaction(oid)

	if result.get("paid") and result.get("data"):
		data = result["data"]
		_stamp_paid(sales_order_name, data)
		payment_review_flag.clear(
			"Sales Order", sales_order_name, settings=settings, correlation_id=correlation_id,
		)
		log_step(
			correlation_id=correlation_id,
			step=STEP_PAID,
			level="Success",
			doc_type="Sales Order",
			linked_doctype="Sales Order",
			linked_docname=sales_order_name,
			response_body=_summarise(data),
		)
		return {"ok": True, "paid": True, "data": data, "reason": None}

	_stamp_unverified(sales_order_name)
	reason = _unverified_reason(oid, result)
	payment_review_flag.flag(
		"Sales Order", sales_order_name, reason, settings=settings, correlation_id=correlation_id,
	)
	log_step(
		correlation_id=correlation_id,
		step=STEP_UNVERIFIED,
		level="Warning",
		doc_type="Sales Order",
		linked_doctype="Sales Order",
		linked_docname=sales_order_name,
		error_message=reason,
	)
	return {"ok": True, "paid": False, "data": None, "reason": reason}


def _stamp_paid(sales_order_name: str, data: dict) -> None:
	"""Persist the confirmed iPay transaction fields on the SO."""
	payer = " ".join(p for p in [data.get("firstname"), data.get("lastname")] if p).strip()
	frappe.db.set_value(
		"Sales Order",
		sales_order_name,
		{
			"wave_ipay_paid": 1,
			"wave_ipay_transaction_code": (data.get("transaction_code") or "").strip() or None,
			"wave_ipay_transaction_amount": flt(data.get("transaction_amount")),
			"wave_ipay_payment_mode": (data.get("payment_mode") or "").strip() or None,
			"wave_ipay_paid_at": _as_datetime(data.get("paid_at")),
			"wave_ipay_payer_name": payer or None,
			"wave_ipay_payer_phone": (data.get("telephone") or "").strip() or None,
			"wave_ipay_verified_at": now_datetime(),
		},
		update_modified=False,
	)


def _stamp_unverified(sales_order_name: str) -> None:
	"""Record that we looked and iPay did not confirm a payment (keep any prior details)."""
	frappe.db.set_value(
		"Sales Order",
		sales_order_name,
		{"wave_ipay_paid": 0, "wave_ipay_verified_at": now_datetime()},
		update_modified=False,
	)


def _as_datetime(value):
	"""Parse iPay's 'YYYY-MM-DD HH:MM:SS' string into a datetime; None on absence/garbage."""
	if not value:
		return None
	try:
		return get_datetime(value)
	except Exception:
		return None


def _unverified_reason(oid: str, result: dict) -> str:
	"""Human-readable reason for the review flag, from the gateway envelope."""
	if not result.get("available"):
		return "iPay app is not installed on this site; payment could not be verified."
	if result.get("error"):
		return f"iPay lookup for oid {oid} failed: {result['error']}."
	return f"iPay has no completed payment for oid {oid} yet."


def _summarise(data: dict) -> dict:
	"""Compact subset of the iPay record for the audit row."""
	return {
		"transaction_code": data.get("transaction_code"),
		"transaction_amount": data.get("transaction_amount"),
		"payment_mode": data.get("payment_mode"),
		"paid_at": data.get("paid_at"),
	}
