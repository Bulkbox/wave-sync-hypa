"""Unit tests for services.payment_status_pusher (issue #120).

Worker-side: master switch check, outbound config gate, HTTP body shape,
never-raise contract, audit-row coverage. wave_client + frappe.enqueue
are patched at the module boundary so no real HTTP fires.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.services import payment_status_pusher
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveOutboundError

DUMMY_BASE_URL = "https://wave.example.com"
DUMMY_API_KEY = "outbound-api-key"
DUMMY_APP_ID = "outbound-app-id"
DUMMY_PE = "PE-2026-0001"
DUMMY_WAVE_ID = "wave-id-aaa"


def _settings(*, enabled: int = 1, full_config: bool = True) -> MagicMock:
	"""Wave Settings stand-in: master toggle + outbound HTTP config."""
	values = {
		"enabled": enabled,
		"wave_api_base_url": DUMMY_BASE_URL if full_config else "",
		"wave_app_id": DUMMY_APP_ID if full_config else "",
	}
	settings = MagicMock(name="WaveSettings")
	settings.get.side_effect = lambda key, default=None: values.get(key, default)
	settings.get_password.return_value = DUMMY_API_KEY if full_config else ""
	return settings


def _pe_doc(name: str = DUMMY_PE) -> SimpleNamespace:
	"""Minimal Payment Entry doc surface for the enqueue entry point."""
	return SimpleNamespace(doctype="Payment Entry", name=name)


class TestEnqueuePaymentStatusPush(FrappeTestCase):
	"""Entry point: one frappe.enqueue, audit row written."""

	def test_enqueues_worker_with_expected_kwargs(self):
		doc = _pe_doc()
		with (
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(payment_status_pusher, "log_step") as mock_log,
		):
			payment_status_pusher.enqueue_payment_status_push(
				doc, DUMMY_WAVE_ID, "COMPLETED", correlation_id="corr-1",
			)

		mock_enqueue.assert_called_once()
		args, kwargs = mock_enqueue.call_args
		self.assertEqual(args[0], payment_status_pusher.WORKER_DOTTED_PATH)
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertEqual(kwargs["pe_name"], DUMMY_PE)
		self.assertEqual(kwargs["wave_order_id"], DUMMY_WAVE_ID)
		self.assertEqual(kwargs["payment_status"], "COMPLETED")
		self.assertEqual(kwargs["correlation_id"], "corr-1")

		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(payment_status_pusher.STEP_ENQUEUED, steps)

	def test_enqueue_failure_logs_error_and_does_not_raise(self):
		doc = _pe_doc()
		with (
			patch.object(frappe, "enqueue", side_effect=RuntimeError("queue down")),
			patch.object(payment_status_pusher, "log_step") as mock_log,
		):
			payment_status_pusher.enqueue_payment_status_push(
				doc, DUMMY_WAVE_ID, "COMPLETED", correlation_id="corr-2",
			)  # must not raise
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(payment_status_pusher.STEP_ENQUEUE_FAILED, steps)


class TestPushPaymentStatusWorker(FrappeTestCase):
	"""Worker job: validate, PATCH /admin/orders/{id} with {paymentStatus: ...}."""

	def _call(self, payment_status: str = "COMPLETED") -> None:
		payment_status_pusher.push_payment_status(
			pe_name=DUMMY_PE,
			wave_order_id=DUMMY_WAVE_ID,
			payment_status=payment_status,
			correlation_id="corr-worker",
		)

	def test_master_switch_off_skips_patch(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(enabled=0)),
			patch.object(payment_status_pusher.wave_client, "patch_order_top_level") as mock_patch,
			patch.object(payment_status_pusher, "log_step") as mock_log,
		):
			self._call()
		mock_patch.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		from wave_sync_hypa.wave_sync_hypa.services.master_switch import STEP_MASTER_DISABLED
		self.assertIn(STEP_MASTER_DISABLED, steps)

	def test_missing_outbound_config_aborts_with_error_row(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(full_config=False)),
			patch.object(payment_status_pusher.wave_client, "patch_order_top_level") as mock_patch,
			patch.object(payment_status_pusher, "log_step") as mock_log,
		):
			self._call()
		mock_patch.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(payment_status_pusher.STEP_ABORTED_MISSING_CONFIG, steps)

	def test_calls_patch_order_top_level_with_payment_status_body(self):
		"""The whole point: body is exactly {paymentStatus: <value>}."""
		response = {
			"_id": DUMMY_WAVE_ID,
			"status": "UNDER_DELIVERY",
			"paymentStatus": "COMPLETED",
			"updatedAt": "2026-05-28T10:00:00.000Z",
		}
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(
				payment_status_pusher.wave_client,
				"patch_order_top_level",
				return_value=response,
			) as mock_patch,
			patch.object(payment_status_pusher, "log_step") as mock_log,
		):
			self._call("COMPLETED")

		mock_patch.assert_called_once_with(
			base_url=DUMMY_BASE_URL,
			api_key=DUMMY_API_KEY,
			app_id=DUMMY_APP_ID,
			order_id=DUMMY_WAVE_ID,
			body={"paymentStatus": "COMPLETED"},
		)
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(payment_status_pusher.STEP_ATTEMPT, steps)
		self.assertIn(payment_status_pusher.STEP_SUCCESS, steps)

	def test_outbound_error_logged_and_swallowed(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(
				payment_status_pusher.wave_client,
				"patch_order_top_level",
				side_effect=WaveOutboundError("HTTP 500: server error"),
			),
			patch.object(payment_status_pusher, "log_step") as mock_log,
		):
			self._call()  # must not raise
		failed = [
			c for c in mock_log.call_args_list
			if c.kwargs.get("step") == payment_status_pusher.STEP_FAILED
		]
		self.assertEqual(len(failed), 1)
		self.assertEqual(failed[0].kwargs.get("level"), "Error")

	def test_unexpected_exception_logged_and_swallowed(self):
		with (
			patch.object(frappe, "get_cached_doc", side_effect=RuntimeError("settings dead")),
			patch.object(payment_status_pusher, "log_step") as mock_log,
		):
			self._call()  # must not raise
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(payment_status_pusher.STEP_UNEXPECTED_ERROR, steps)
