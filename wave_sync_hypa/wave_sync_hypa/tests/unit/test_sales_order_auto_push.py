"""Unit tests for handlers.order_status.maybe_auto_push_to_wave.

The on_submit auto-push hook that fires after a Sales Order reaches
docstatus=1 (via direct Submit OR workflow approval). Three guard
clauses keep it safe and idempotent; the actual push goes through a
worker (frappe.enqueue) so submit returns fast.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers import order_status

SO_NAME = "SAL-ORD-2026-AUTO-1"


def _settings(*, push_enabled: int = 1) -> MagicMock:
	"""Wave Settings stand-in carrying only the auto-push kill-switch."""
	settings = MagicMock(name="WaveSettings")
	settings.get.side_effect = lambda key, default=None: {
		"enabled": 1,
		"erp_to_wave_push_enabled": push_enabled,
	}.get(key, default)
	return settings


def _so(*, wave_order_id: str = "", wave_origin: str = "") -> MagicMock:
	"""Submitted Sales Order doc stand-in."""
	doc = MagicMock(name="SalesOrderDoc")
	doc.doctype = "Sales Order"
	doc.name = SO_NAME
	doc.wave_order_id = wave_order_id
	doc.get.side_effect = lambda key, default=None: {
		"wave_order_id": wave_order_id,
		"wave_origin": wave_origin,
	}.get(key, default)
	return doc


class TestMaybeAutoPushToWave(FrappeTestCase):
	"""Five branches: disabled / already pushed / wave-origin / happy / enqueue fail."""

	def test_kill_switch_off_logs_skipped_and_does_not_enqueue(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(push_enabled=0)),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(order_status, "log_step") as mock_log,
		):
			order_status.maybe_auto_push_to_wave(_so())
		mock_enqueue.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(order_status.STEP_AUTO_PUSH_SKIPPED_DISABLED, steps)

	def test_already_pushed_so_logs_skipped_and_does_not_enqueue(self):
		"""SOs with wave_order_id already set must never be double-pushed."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(order_status, "log_step") as mock_log,
		):
			order_status.maybe_auto_push_to_wave(_so(wave_order_id="wave-existing-id"))
		mock_enqueue.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(order_status.STEP_AUTO_PUSH_SKIPPED_ALREADY_PUSHED, steps)

	def test_wave_webhook_origin_so_does_not_get_pushed_back(self):
		"""Wave-originated SOs (intake from webhook) have wave_origin='Wave Webhook' -> skip."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(order_status, "log_step") as mock_log,
		):
			order_status.maybe_auto_push_to_wave(_so(wave_origin="Wave Webhook"))
		mock_enqueue.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(order_status.STEP_AUTO_PUSH_SKIPPED_WAVE_ORIGIN, steps)

	def test_happy_path_enqueues_push_worker_with_correct_args(self):
		"""All guards pass -> frappe.enqueue called with push_so_to_wave dotted path + so_name + correlation_id."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(order_status, "log_step") as mock_log,
		):
			order_status.maybe_auto_push_to_wave(_so())
		mock_enqueue.assert_called_once()
		args, kwargs = mock_enqueue.call_args
		# Dotted path is the first positional or named arg depending on frappe version; check both.
		dotted = args[0] if args else kwargs.get("method")
		self.assertEqual(dotted, order_status.WAVE_ORDER_CREATOR_DOTTED_PATH)
		self.assertEqual(kwargs["so_name"], SO_NAME)
		self.assertTrue(kwargs["correlation_id"])  # non-empty
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertEqual(kwargs["job_name"], f"erp_to_wave_push:{SO_NAME}")
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(order_status.STEP_AUTO_PUSH_ENQUEUED, steps)

	def test_enqueue_failure_logs_error_and_does_not_propagate(self):
		"""If frappe.enqueue itself raises (e.g. Redis down), log Error and return without crashing submit."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "enqueue", side_effect=RuntimeError("redis dead")),
			patch.object(frappe, "get_traceback", return_value=""),
			patch.object(order_status, "log_step") as mock_log,
		):
			# Must not raise.
			order_status.maybe_auto_push_to_wave(_so())
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(order_status.STEP_AUTO_PUSH_ENQUEUE_FAILED, steps)
		# The success-row enqueued step must NOT be logged when enqueue failed.
		self.assertNotIn(order_status.STEP_AUTO_PUSH_ENQUEUED, steps)


class TestMaybeAutoPushCustomerGate(FrappeTestCase):
	"""A disabled customer skips the auto-push enqueue entirely."""

	def test_disabled_customer_does_not_enqueue(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(order_status.wave_customer_resolver, "is_erp_to_wave_disabled", return_value=True),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(order_status, "log_step") as mock_log,
		):
			order_status.maybe_auto_push_to_wave(_so())
		mock_enqueue.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(order_status.wave_customer_resolver.STEP_ERP_TO_WAVE_CUSTOMER_DISABLED, steps)
