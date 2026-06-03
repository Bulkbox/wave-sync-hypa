"""Unit tests for services.pick_list_amend_resetter (issue #113).

Worker-side tests mirror the pick_list_batch_pusher pattern: master switch
check, outbound config gate, HTTP call shape, never-raise contract,
audit-row coverage. wave_client + frappe.enqueue are patched at the module
boundary so no real HTTP fires.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.services import pick_list_amend_resetter
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveOutboundError

DUMMY_BASE_URL = "https://wave.example.com"
DUMMY_API_KEY = "outbound-api-key"
DUMMY_APP_ID = "outbound-app-id"
DUMMY_PL = "PICK-2026-AMEND-1"
DUMMY_WAVE_ID_A = "wave-id-aaa"
DUMMY_WAVE_ID_B = "wave-id-bbb"


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


def _doc(name: str = DUMMY_PL) -> SimpleNamespace:
	"""Minimal Pick List doc surface for the enqueue entry point."""
	return SimpleNamespace(doctype="Pick List", name=name)


class TestEnqueuePickerStateReset(FrappeTestCase):
	"""Entry point: one frappe.enqueue per wave_order_id, audit row written."""

	def test_enqueues_one_worker_per_wave_order_id(self):
		doc = _doc()
		settings = _settings()
		with (
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(pick_list_amend_resetter, "log_step") as mock_log,
		):
			pick_list_amend_resetter.enqueue_picker_state_reset(
				doc, [DUMMY_WAVE_ID_A, DUMMY_WAVE_ID_B], settings,
			)

		self.assertEqual(mock_enqueue.call_count, 2)
		for call in mock_enqueue.call_args_list:
			self.assertEqual(call.args[0], pick_list_amend_resetter.WORKER_DOTTED_PATH)
			self.assertTrue(call.kwargs["enqueue_after_commit"])
			self.assertEqual(call.kwargs["pick_list_name"], DUMMY_PL)
		wave_ids = {c.kwargs["wave_order_id"] for c in mock_enqueue.call_args_list}
		self.assertEqual(wave_ids, {DUMMY_WAVE_ID_A, DUMMY_WAVE_ID_B})

		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertEqual(steps.count(pick_list_amend_resetter.STEP_ENQUEUED), 2)

	def test_enqueue_failure_logs_error_and_continues_to_next_wave_id(self):
		"""frappe.enqueue raising for one wave id doesn't block the others."""
		doc = _doc()

		def _side_effect(*args, **kwargs):
			if kwargs.get("wave_order_id") == DUMMY_WAVE_ID_A:
				raise RuntimeError("queue down")

		with (
			patch.object(frappe, "enqueue", side_effect=_side_effect) as mock_enqueue,
			patch.object(pick_list_amend_resetter, "log_step") as mock_log,
		):
			pick_list_amend_resetter.enqueue_picker_state_reset(
				doc, [DUMMY_WAVE_ID_A, DUMMY_WAVE_ID_B], _settings(),
			)

		self.assertEqual(mock_enqueue.call_count, 2)  # both attempts made
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pick_list_amend_resetter.STEP_ENQUEUE_FAILED, steps)
		# The good wave id still gets its enqueued audit row.
		self.assertIn(pick_list_amend_resetter.STEP_ENQUEUED, steps)


class TestResetPickerStateWorker(FrappeTestCase):
	"""Worker job: validate, PATCH /admin/orders/{id} with null picker payload."""

	def _call(self, wave_order_id: str = DUMMY_WAVE_ID_A) -> None:
		pick_list_amend_resetter.reset_picker_state(
			pick_list_name=DUMMY_PL,
			wave_order_id=wave_order_id,
			correlation_id="corr-amend-1",
		)

	def test_master_switch_off_skips_patch(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(enabled=0)),
			patch.object(pick_list_amend_resetter.wave_client, "patch_order_top_level") as mock_patch,
			patch.object(pick_list_amend_resetter, "log_step") as mock_log,
		):
			self._call()
		mock_patch.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		# Reuses the canonical master-disabled step constant so existing log dashboards filter it.
		from wave_sync_hypa.wave_sync_hypa.services.master_switch import STEP_MASTER_DISABLED
		self.assertIn(STEP_MASTER_DISABLED, steps)

	def test_missing_outbound_config_aborts_with_error_row(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(full_config=False)),
			patch.object(pick_list_amend_resetter.wave_client, "patch_order_top_level") as mock_patch,
			patch.object(pick_list_amend_resetter, "log_step") as mock_log,
		):
			self._call()
		mock_patch.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pick_list_amend_resetter.STEP_ABORTED_MISSING_CONFIG, steps)

	def test_calls_patch_order_top_level_with_null_picker_body(self):
		"""The whole point: body is exactly {pickerStatus: None, picking: None}."""
		response = {
			"_id": DUMMY_WAVE_ID_A,
			"status": "ACCEPTED",
			"pickerStatus": None,
			"picking": None,
			"updatedAt": "2026-05-28T10:00:00.000Z",
		}
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(
				pick_list_amend_resetter.wave_client,
				"patch_order_top_level",
				return_value=response,
			) as mock_patch,
			patch.object(pick_list_amend_resetter, "log_step") as mock_log,
		):
			self._call()

		mock_patch.assert_called_once_with(
			base_url=DUMMY_BASE_URL,
			api_key=DUMMY_API_KEY,
			app_id=DUMMY_APP_ID,
			order_id=DUMMY_WAVE_ID_A,
			body={"pickerStatus": None, "picking": None},
		)
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pick_list_amend_resetter.STEP_ATTEMPT, steps)
		self.assertIn(pick_list_amend_resetter.STEP_SUCCESS, steps)

	def test_success_audit_row_summarises_response(self):
		response = {
			"_id": DUMMY_WAVE_ID_A,
			"status": "ACCEPTED",
			"pickerStatus": None,
			"picking": None,
			"updatedAt": "2026-05-28T10:00:00.000Z",
			"products": [{"productId": "noise"}],  # excluded from summary
		}
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(
				pick_list_amend_resetter.wave_client,
				"patch_order_top_level",
				return_value=response,
			),
			patch.object(pick_list_amend_resetter, "log_step") as mock_log,
		):
			self._call()

		success = [
			c for c in mock_log.call_args_list
			if c.kwargs.get("step") == pick_list_amend_resetter.STEP_SUCCESS
		]
		self.assertEqual(len(success), 1)
		summary = success[0].kwargs["response_body"]
		self.assertEqual(summary["order_id"], DUMMY_WAVE_ID_A)
		self.assertEqual(summary["status"], "ACCEPTED")
		self.assertIsNone(summary["picker_status"])
		self.assertIsNone(summary["picking"])
		self.assertNotIn("products", summary)

	def test_residual_picker_state_logs_warning_not_success(self):
		"""Wave returns 2xx but pickerStatus/picking come back populated -> mismatch Warning."""
		response = {
			"_id": DUMMY_WAVE_ID_A,
			"status": "ACCEPTED",
			"pickerStatus": "COLLECTED",  # reset did not take
			"picking": {"completedAt": "2026-05-28T09:00:00.000Z"},
			"updatedAt": "2026-05-28T10:00:00.000Z",
		}
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(
				pick_list_amend_resetter.wave_client, "patch_order_top_level", return_value=response,
			),
			patch.object(pick_list_amend_resetter, "log_step") as mock_log,
		):
			self._call()

		mismatch = [
			c for c in mock_log.call_args_list
			if c.kwargs.get("step") == pick_list_amend_resetter.STEP_RESPONSE_MISMATCH
		]
		self.assertEqual(len(mismatch), 1)
		self.assertEqual(mismatch[0].kwargs.get("level"), "Warning")
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertNotIn(pick_list_amend_resetter.STEP_SUCCESS, steps)

	def test_non_dict_response_treated_as_mismatch(self):
		"""Unparseable / non-dict response can't confirm the reset -> mismatch, not success."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(
				pick_list_amend_resetter.wave_client, "patch_order_top_level", return_value="<text>",
			),
			patch.object(pick_list_amend_resetter, "log_step") as mock_log,
		):
			self._call()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pick_list_amend_resetter.STEP_RESPONSE_MISMATCH, steps)
		self.assertNotIn(pick_list_amend_resetter.STEP_SUCCESS, steps)

	def test_dict_without_order_id_is_unverifiable_not_success(self):
		"""A garbage 2xx body (no _id, no picker keys) must not read as a clean reset."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(
				pick_list_amend_resetter.wave_client,
				"patch_order_top_level",
				return_value={"raw": "<html>upstream error</html>"},
			),
			patch.object(pick_list_amend_resetter, "log_step") as mock_log,
		):
			self._call()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pick_list_amend_resetter.STEP_RESPONSE_MISMATCH, steps)
		self.assertNotIn(pick_list_amend_resetter.STEP_SUCCESS, steps)

	def test_outbound_error_logged_and_swallowed(self):
		"""HTTP failure -> STEP_FAILED Error row, worker does not raise."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(
				pick_list_amend_resetter.wave_client,
				"patch_order_top_level",
				side_effect=WaveOutboundError("HTTP 500: server error"),
			),
			patch.object(pick_list_amend_resetter, "log_step") as mock_log,
		):
			self._call()  # must not raise
		failed = [
			c for c in mock_log.call_args_list
			if c.kwargs.get("step") == pick_list_amend_resetter.STEP_FAILED
		]
		self.assertEqual(len(failed), 1)
		self.assertEqual(failed[0].kwargs.get("level"), "Error")

	def test_unexpected_exception_logged_and_swallowed(self):
		"""Any unexpected error (e.g. settings read blows up) -> STEP_UNEXPECTED_ERROR, no raise."""
		with (
			patch.object(frappe, "get_cached_doc", side_effect=RuntimeError("settings dead")),
			patch.object(pick_list_amend_resetter, "log_step") as mock_log,
		):
			self._call()  # must not raise
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pick_list_amend_resetter.STEP_UNEXPECTED_ERROR, steps)
