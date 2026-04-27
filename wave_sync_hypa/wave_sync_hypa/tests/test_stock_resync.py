"""Unit tests for the manual stock-resync coordinator and endpoint.

Covers the API endpoint (`api.wave_settings.start_full_resync`), the
coordinator that runs in the worker (`services.stock_resync.enqueue_full_resync_jobs`),
and the iteration / chunking helpers. RQ + DB are mocked so tests don't
side-effect the queue or touch real Items.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.api import wave_settings as endpoint
from wave_sync_hypa.wave_sync_hypa.services import stock_resync

DUMMY_DEFAULT_WAREHOUSE = "Stores - WAVE"


def _stub_settings(*, enabled: bool = True, default_warehouse: str = DUMMY_DEFAULT_WAREHOUSE,
				   base_url: str = "https://wave.example.com",
				   app_id: str = "test-app-id",
				   store_id: str = "1",
				   api_key: str = "test-api-key") -> SimpleNamespace:
	"""Build a settings stand-in usable by both the endpoint (full Doc) and the coordinator (cached doc)."""
	stub = SimpleNamespace()
	stub.outbound_stock_sync_enabled = 1 if enabled else 0
	stub.default_warehouse = default_warehouse
	stub.wave_api_base_url = base_url
	stub.wave_app_id = app_id
	stub.wave_store_id = store_id
	stub._api_key = api_key

	values = {
		"outbound_stock_sync_enabled": stub.outbound_stock_sync_enabled,
		"default_warehouse": default_warehouse,
		"wave_api_base_url": base_url,
		"wave_app_id": app_id,
		"wave_store_id": store_id,
	}
	stub.get = lambda key, default=None: values.get(key, default)
	stub.get_password = lambda key, raise_exception=False: api_key
	return stub


class TestStartFullResyncEndpoint(FrappeTestCase):
	"""Validate the click-time guards and the shape of the response."""

	def test_full_mode_returns_batch_and_count(self):
		"""No item_codes supplied → endpoint enqueues coordinator and reports total count."""
		settings = _stub_settings()
		with (
			patch.object(frappe, "only_for"),
			patch.object(frappe, "get_doc", return_value=settings),
			patch.object(stock_resync, "count_eligible_items", return_value=42),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(endpoint, "log_step"),
		):
			result = endpoint.start_full_resync()

		self.assertTrue(result["ok"])
		self.assertEqual(result["item_count_estimate"], 42)
		self.assertTrue(result["batch_id"])
		mock_enqueue.assert_called_once()
		_, kwargs = mock_enqueue.call_args
		self.assertEqual(kwargs["job_id"], stock_resync.RESYNC_JOB_NAME)
		self.assertIsNone(kwargs["item_codes"])

	def test_explicit_mode_passes_cleaned_list_to_coordinator(self):
		"""item_codes=['A','','B'] → empty entries dropped, coordinator queued with ['A','B']."""
		settings = _stub_settings()
		with (
			patch.object(frappe, "only_for"),
			patch.object(frappe, "get_doc", return_value=settings),
			patch.object(stock_resync, "count_eligible_items", return_value=2),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(endpoint, "log_step"),
		):
			result = endpoint.start_full_resync(item_codes=["A", "", "B"])

		self.assertEqual(result["item_count_estimate"], 2)
		self.assertEqual(mock_enqueue.call_args.kwargs["item_codes"], ["A", "B"])

	def test_refuses_when_kill_switch_off(self):
		"""outbound_stock_sync_enabled=0 → throws, no enqueue."""
		settings = _stub_settings(enabled=False)
		with (
			patch.object(frappe, "only_for"),
			patch.object(frappe, "get_doc", return_value=settings),
			patch.object(frappe, "enqueue") as mock_enqueue,
		):
			with self.assertRaises(frappe.ValidationError):
				endpoint.start_full_resync()
		mock_enqueue.assert_not_called()

	def test_refuses_when_required_outbound_field_missing(self):
		"""Empty wave_app_id → throws, no enqueue."""
		settings = _stub_settings(app_id="")
		with (
			patch.object(frappe, "only_for"),
			patch.object(frappe, "get_doc", return_value=settings),
			patch.object(frappe, "enqueue") as mock_enqueue,
		):
			with self.assertRaises(frappe.ValidationError):
				endpoint.start_full_resync()
		mock_enqueue.assert_not_called()

	def test_refuses_empty_explicit_list(self):
		"""item_codes=[] → throws (operator probably meant 'all', but be explicit)."""
		settings = _stub_settings()
		with (
			patch.object(frappe, "only_for"),
			patch.object(frappe, "get_doc", return_value=settings),
		):
			with self.assertRaises(frappe.ValidationError):
				endpoint.start_full_resync(item_codes=[])


class TestResyncCoordinator(FrappeTestCase):
	"""Verify the worker-side coordinator iterates correctly and tolerates per-item failures."""

	def test_enqueues_one_job_per_eligible_item_with_shared_batch(self):
		"""Three items returned by Item query → three frappe.enqueue calls, all carrying batch_id."""
		settings = _stub_settings()
		items = [{"name": f"SKU-{i}"} for i in range(3)]
		with (
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe, "get_all", side_effect=[items, []]),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(stock_resync, "log_step"),
		):
			stock_resync.enqueue_full_resync_jobs("batch-A")

		self.assertEqual(mock_enqueue.call_count, 3)
		for call in mock_enqueue.call_args_list:
			self.assertEqual(call.kwargs["batch_id"], "batch-A")
			self.assertTrue(call.kwargs["job_id"].startswith("wave-sync:stock:"))
			self.assertTrue(call.kwargs["deduplicate"])

	def test_logs_started_and_completed_with_counters(self):
		"""Successful run logs started + completed; completed carries queued / enqueue_failed counters."""
		settings = _stub_settings()
		items = [{"name": "SKU-1"}, {"name": "SKU-2"}]
		with (
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe, "get_all", side_effect=[items, []]),
			patch.object(frappe, "enqueue"),
			patch.object(stock_resync, "log_step") as mock_log,
		):
			stock_resync.enqueue_full_resync_jobs("batch-B")

		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(stock_resync.STEP_RESYNC_STARTED, steps)
		self.assertIn(stock_resync.STEP_RESYNC_COMPLETED, steps)
		completed = next(c for c in mock_log.call_args_list
						 if c.kwargs.get("step") == stock_resync.STEP_RESYNC_COMPLETED)
		body = completed.kwargs["request_body"]
		self.assertEqual(body["queued"], 2)
		self.assertEqual(body["enqueue_failed"], 0)

	def test_one_enqueue_failure_does_not_stop_loop(self):
		"""If frappe.enqueue raises on item #2, items #3 + #4 are still queued and the failure is logged."""
		settings = _stub_settings()
		items = [{"name": f"SKU-{i}"} for i in range(4)]

		call_count = {"n": 0}

		def flaky_enqueue(*args, **kwargs):
			call_count["n"] += 1
			if call_count["n"] == 2:
				raise RuntimeError("redis blip")
			return None

		with (
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe, "get_all", side_effect=[items, []]),
			patch.object(frappe, "enqueue", side_effect=flaky_enqueue),
			patch.object(stock_resync, "log_step") as mock_log,
		):
			stock_resync.enqueue_full_resync_jobs("batch-C")

		# All four items were attempted (so the loop did not break on the second item).
		self.assertEqual(call_count["n"], 4)

		# The failure is logged at the right step.
		failure_logs = [
			c for c in mock_log.call_args_list
			if c.kwargs.get("step") == stock_resync.STEP_RESYNC_ITEM_ENQUEUE_FAILED
		]
		self.assertEqual(len(failure_logs), 1)

		# Completed counters reflect the partial outcome: 3 queued, 1 enqueue_failed.
		completed = next(c for c in mock_log.call_args_list
						 if c.kwargs.get("step") == stock_resync.STEP_RESYNC_COMPLETED)
		self.assertEqual(completed.kwargs["request_body"]["queued"], 3)
		self.assertEqual(completed.kwargs["request_body"]["enqueue_failed"], 1)

	def test_aborts_when_settings_disabled_at_run_time(self):
		"""Kill-switch flipped between request and run → aborted log row, no per-item enqueues."""
		settings = _stub_settings(enabled=False)
		with (
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(stock_resync, "log_step") as mock_log,
		):
			stock_resync.enqueue_full_resync_jobs("batch-D")

		mock_enqueue.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(stock_resync.STEP_RESYNC_ABORTED, steps)

	def test_aborts_when_default_warehouse_blank(self):
		"""Empty default_warehouse → aborted, no enqueues."""
		settings = _stub_settings(default_warehouse="")
		with (
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(stock_resync, "log_step") as mock_log,
		):
			stock_resync.enqueue_full_resync_jobs("batch-E")

		mock_enqueue.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(stock_resync.STEP_RESYNC_ABORTED, steps)

	def test_explicit_codes_passes_filter_to_query(self):
		"""When called with item_codes, the Item query carries an `in` filter for them."""
		settings = _stub_settings()
		captured: dict = {}

		def capture_get_all(doctype, **kwargs):
			captured["filters"] = kwargs.get("filters")
			return [{"name": "ALPHA"}]

		with (
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe, "get_all", side_effect=capture_get_all),
			patch.object(frappe, "enqueue"),
			patch.object(stock_resync, "log_step"),
		):
			stock_resync.enqueue_full_resync_jobs("batch-F", item_codes=["ALPHA", "BETA"])

		self.assertEqual(captured["filters"]["name"], ["in", ["ALPHA", "BETA"]])
		self.assertEqual(captured["filters"]["disabled"], 0)
		self.assertEqual(captured["filters"]["is_stock_item"], 1)

	def test_coordinator_wraps_unexpected_exception(self):
		"""If the coordinator itself blows up, log stock_sync_resync_failed and return cleanly."""
		with (
			patch.object(frappe, "get_cached_doc", side_effect=RuntimeError("settings dead")),
			patch.object(stock_resync, "log_step") as mock_log,
		):
			# Must not raise.
			stock_resync.enqueue_full_resync_jobs("batch-G")

		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(stock_resync.STEP_RESYNC_FAILED, steps)
