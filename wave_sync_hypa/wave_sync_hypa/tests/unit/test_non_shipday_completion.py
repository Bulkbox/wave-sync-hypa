"""Unit tests for non-Shipday order completion (issue #118).

Two entry points push Wave status=COMPLETED: the manual SO button
(api.sales_order.mark_completed_on_wave) and the opt-in auto-push on a
fully-settled Payment Entry submit. Collaborators are patched at the module
boundary so no real HTTP / DB writes fire.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.api import sales_order as so_api
from wave_sync_hypa.wave_sync_hypa.handlers import payment_entry


def _pe():
	doc = SimpleNamespace(doctype="Payment Entry", name="PE-1")
	values = {"payment_type": "Receive", "wave_order_id": "W1"}
	doc.get = lambda key, default=None: values.get(key, default)
	return doc


def _so(wave_order_id="W1"):
	doc = SimpleNamespace(doctype="Sales Order", name="SO-1")
	doc.check_permission = lambda perm: None
	doc.get = lambda key, default=None: {"wave_order_id": wave_order_id}.get(key, default)
	return doc


class TestPaymentEntryAutoCompletion(FrappeTestCase):
	"""A fully-settled PE pushes COMPLETED only when the manager opted in."""

	def _run(self, *, mode_on, status):
		with (
			patch.object(payment_entry, "skip_if_disabled", return_value=False),
			patch.object(payment_entry, "_collect_distinct_wave_order_ids", return_value=["W1"]),
			patch.object(
				payment_entry.payment_status_resolver, "resolve_status_for_wave_order", return_value=status
			),
			patch.object(payment_entry.payment_status_pusher, "enqueue_payment_status_push"),
			patch.object(payment_entry, "_complete_on_payment_entry", return_value=mode_on),
			patch.object(payment_entry.order_status, "dispatch_with_wave_order_ids") as mock_dispatch,
			patch.object(payment_entry, "log_step"),
		):
			payment_entry.on_payment_entry_submit(_pe())
		return mock_dispatch

	def test_completes_when_mode_on_and_fully_settled(self):
		dispatch = self._run(mode_on=True, status="COMPLETED")
		dispatch.assert_called_once()
		args, kwargs = dispatch.call_args
		self.assertEqual(args[2], ["W1"])
		self.assertEqual(kwargs["forced_payload"], {"status": "COMPLETED"})

	def test_no_completion_when_mode_off(self):
		self._run(mode_on=False, status="COMPLETED").assert_not_called()

	def test_no_completion_when_not_fully_settled(self):
		self._run(mode_on=True, status=None).assert_not_called()


class TestMarkCompletedOnWaveApi(FrappeTestCase):
	"""The manual button endpoint pushes COMPLETED for a Wave-linked SO."""

	def test_dispatches_completed(self):
		with (
			patch.object(frappe, "get_doc", return_value=_so()),
			patch.object(so_api.order_status, "dispatch_with_wave_order_ids") as mock_dispatch,
			patch.object(frappe.db, "commit"),
		):
			result = so_api.mark_completed_on_wave("SO-1")
		self.assertTrue(result["ok"])
		args, kwargs = mock_dispatch.call_args
		self.assertEqual(kwargs["forced_payload"], {"status": "COMPLETED"})

	def test_no_wave_order_id_returns_not_ok(self):
		with (
			patch.object(frappe, "get_doc", return_value=_so(wave_order_id="")),
			patch.object(so_api.order_status, "dispatch_with_wave_order_ids") as mock_dispatch,
		):
			result = so_api.mark_completed_on_wave("SO-1")
		self.assertFalse(result["ok"])
		mock_dispatch.assert_not_called()
