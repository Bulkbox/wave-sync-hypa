"""Unit tests for api.shipday_intercept.order_stage (issue #117).

The wrapper override that n8n now hits via override_whitelisted_methods.
Confirms:
  - Upstream runs unconditionally; its side effects always land.
  - Wave dispatch ONLY fires for "Delivered" + SO with wave_order_id.
  - "Failed" / "Partial Delivery" never push to Wave.
  - Wave-side exception is swallowed; upstream return preserved.
  - Upstream's own exception is NOT swallowed (CS-Cart sync errors must
    still bubble up to n8n).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.api import shipday_intercept
from wave_sync_hypa.wave_sync_hypa.handlers import order_status

DUMMY_DN = "MAT-DN-2026-00001"
DUMMY_SO = "SAL-ORD-2026-00001"
WAVE_ID = "wave-id-aaa"


def _settings(stages: str = "Delivered") -> SimpleNamespace:
	"""Mock Wave Settings whose shipday_completion_stages field holds `stages`."""
	s = SimpleNamespace()
	s.get = lambda key, default=None: {"shipday_completion_stages": stages}.get(key, default)
	return s


def _upstream_result(new_stage: str, sales_order: str = DUMMY_SO) -> dict:
	return {
		"status": "success",
		"message": f"Order stage for Sales Order {sales_order} updated to '{new_stage}'",
		"sales_order": sales_order,
		"new_stage": new_stage,
	}


class TestShipdayInterceptOrderStage(FrappeTestCase):
	"""Wrapper contract: upstream-first, conditional Wave push, never break upstream."""

	def setUp(self):
		# Hold the master kill switch open so these tests exercise the wrapper's
		# upstream-first / conditional-dispatch contract; the disabled-path
		# (upstream still runs, Wave push skipped) is covered in
		# test_master_switch.TestDecisionLayerSkipsEnqueueWhenMasterOff.
		guard = patch.object(shipday_intercept, "skip_if_disabled", return_value=False)
		guard.start()
		self.addCleanup(guard.stop)
		# Default completion stages = "Delivered" (the shipped default); individual
		# tests override get_cached_doc to configure other stages.
		settings_guard = patch.object(frappe, "get_cached_doc", return_value=_settings("Delivered"))
		settings_guard.start()
		self.addCleanup(settings_guard.stop)

	def test_delivered_with_wave_order_id_dispatches_completed(self):
		"""Delivered + SO has wave_order_id -> dispatch fires with forced COMPLETED."""
		expected = _upstream_result("Delivered")
		with (
			patch.object(shipday_intercept, "_upstream_order_stage", return_value=expected) as mock_up,
			patch.object(frappe.db, "get_value", return_value=WAVE_ID),
			patch.object(frappe, "get_doc"),
			patch.object(order_status, "dispatch_with_wave_order_ids") as mock_dispatch,
			patch.object(shipday_intercept, "log_step") as mock_log,
		):
			result = shipday_intercept.order_stage(DUMMY_DN)

		mock_up.assert_called_once_with(DUMMY_DN)
		mock_dispatch.assert_called_once()
		args, kwargs = mock_dispatch.call_args
		self.assertEqual(args[1], shipday_intercept.EVENT_SHIPDAY_DELIVERED)
		self.assertEqual(args[2], [WAVE_ID])
		self.assertEqual(kwargs["forced_payload"], {"status": "COMPLETED"})
		# Upstream return value handed back unchanged.
		self.assertEqual(result, expected)
		# Success row written.
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(shipday_intercept.STEP_DISPATCHED, steps)

	def test_failed_stage_does_not_dispatch_but_upstream_still_runs(self):
		"""new_stage=Failed -> upstream ran (custom_order_stage='Failed' on the SO), no Wave push."""
		expected = _upstream_result("Failed")
		with (
			patch.object(shipday_intercept, "_upstream_order_stage", return_value=expected) as mock_up,
			patch.object(frappe.db, "get_value") as mock_get_value,
			patch.object(order_status, "dispatch_with_wave_order_ids") as mock_dispatch,
			patch.object(shipday_intercept, "log_step") as mock_log,
		):
			result = shipday_intercept.order_stage(DUMMY_DN)

		mock_up.assert_called_once_with(DUMMY_DN)  # upstream side effects landed
		mock_dispatch.assert_not_called()
		mock_get_value.assert_not_called()  # short-circuited before SO lookup
		mock_log.assert_not_called()
		self.assertEqual(result, expected)

	def test_partial_delivery_not_dispatched_when_not_configured(self):
		"""Default stages = 'Delivered' only -> Partial Delivery does not push."""
		expected = _upstream_result("Partial Delivery")
		with (
			patch.object(shipday_intercept, "_upstream_order_stage", return_value=expected),
			patch.object(order_status, "dispatch_with_wave_order_ids") as mock_dispatch,
		):
			result = shipday_intercept.order_stage(DUMMY_DN)
		mock_dispatch.assert_not_called()
		self.assertEqual(result, expected)

	def test_partial_delivery_dispatches_when_configured(self):
		"""When 'Partial Delivery' is in shipday_completion_stages, it pushes COMPLETED."""
		expected = _upstream_result("Partial Delivery")
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings("Delivered\nPartial Delivery")),
			patch.object(shipday_intercept, "_upstream_order_stage", return_value=expected),
			patch.object(frappe.db, "get_value", return_value=WAVE_ID),
			patch.object(frappe, "get_doc"),
			patch.object(order_status, "dispatch_with_wave_order_ids") as mock_dispatch,
			patch.object(shipday_intercept, "log_step"),
		):
			result = shipday_intercept.order_stage(DUMMY_DN)
		mock_dispatch.assert_called_once()
		self.assertEqual(mock_dispatch.call_args.kwargs["forced_payload"], {"status": "COMPLETED"})
		self.assertEqual(result, expected)

	def test_stage_match_is_trimmed_and_case_insensitive(self):
		"""A stray-cased / padded stage still matches a configured stage."""
		expected = _upstream_result("  DELIVERED ")
		with (
			patch.object(shipday_intercept, "_upstream_order_stage", return_value=expected),
			patch.object(frappe.db, "get_value", return_value=WAVE_ID),
			patch.object(frappe, "get_doc"),
			patch.object(order_status, "dispatch_with_wave_order_ids") as mock_dispatch,
			patch.object(shipday_intercept, "log_step"),
		):
			shipday_intercept.order_stage(DUMMY_DN)
		mock_dispatch.assert_called_once()

	def test_default_stages_are_delivered_and_partial_delivery(self):
		"""Empty setting -> default completes both Delivered and Partial Delivery."""
		self.assertEqual(
			shipday_intercept._completion_stages(_settings("")),
			{"delivered", "partialdelivery"},
		)

	def test_norm_drops_all_whitespace_and_casefolds(self):
		"""Normalisation removes every space and lowercases, so spacing/casing never blocks a match."""
		self.assertEqual(shipday_intercept._norm("  pArTiAl   Delivery "), "partialdelivery")
		self.assertEqual(shipday_intercept._norm("DELIVERED"), "delivered")
		self.assertEqual(shipday_intercept._norm(None), "")

	def test_delivered_but_so_lacks_wave_order_id_does_not_dispatch(self):
		"""Non-Wave Sales Order: upstream ran, but no Wave push."""
		expected = _upstream_result("Delivered")
		with (
			patch.object(shipday_intercept, "_upstream_order_stage", return_value=expected),
			patch.object(frappe.db, "get_value", return_value=None),
			patch.object(order_status, "dispatch_with_wave_order_ids") as mock_dispatch,
			patch.object(shipday_intercept, "log_step") as mock_log,
		):
			result = shipday_intercept.order_stage(DUMMY_DN)

		mock_dispatch.assert_not_called()
		mock_log.assert_not_called()
		self.assertEqual(result, expected)

	def test_wave_dispatch_exception_is_swallowed_and_logged(self):
		"""Wave dispatch raising must NOT break upstream's return value."""
		expected = _upstream_result("Delivered")
		with (
			patch.object(shipday_intercept, "_upstream_order_stage", return_value=expected),
			patch.object(frappe.db, "get_value", return_value=WAVE_ID),
			patch.object(frappe, "get_doc"),
			patch.object(
				order_status,
				"dispatch_with_wave_order_ids",
				side_effect=RuntimeError("Wave is down"),
			),
			patch.object(shipday_intercept, "log_step") as mock_log,
		):
			result = shipday_intercept.order_stage(DUMMY_DN)  # must not raise

		# Upstream return value preserved intact.
		self.assertEqual(result, expected)
		# Error audit row written.
		failed = [c for c in mock_log.call_args_list if c.kwargs.get("step") == shipday_intercept.STEP_FAILED]
		self.assertEqual(len(failed), 1)
		self.assertEqual(failed[0].kwargs.get("level"), "Error")

	def test_upstream_exception_is_not_swallowed(self):
		"""If upstream (CS-Cart sync etc.) errors, the wrapper must propagate -> n8n sees the real failure."""
		with (
			patch.object(
				shipday_intercept,
				"_upstream_order_stage",
				side_effect=RuntimeError("CS-Cart sync failed"),
			),
			patch.object(order_status, "dispatch_with_wave_order_ids") as mock_dispatch,
		):
			with self.assertRaises(RuntimeError):
				shipday_intercept.order_stage(DUMMY_DN)
		mock_dispatch.assert_not_called()
