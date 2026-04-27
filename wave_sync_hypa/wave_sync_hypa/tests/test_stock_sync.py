"""Unit tests for the outbound stock-sync pipeline.

Covers the SLE on_submit handler (filters + enqueue), the stock_pusher worker
(payload shape + log emission), and the wave_client HTTP wrapper. All HTTP is
mocked at requests.post so tests never touch the real Wave API.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers import stock_sync as handler
from wave_sync_hypa.wave_sync_hypa.services import stock_pusher, wave_client
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveOutboundError

DUMMY_BASE_URL = "https://wave.example.com"
DUMMY_API_KEY = "test-api-key-123"
DUMMY_APP_ID = "test-app-id"
DUMMY_STORE_ID = "1"
DUMMY_ITEM = "TEST-SKU-001"
DUMMY_DEFAULT_WAREHOUSE = "Stores - WAVE"
DUMMY_OTHER_WAREHOUSE = "WIP - WAVE"


def _stub_settings(*, enabled: bool = True, default_warehouse: str = DUMMY_DEFAULT_WAREHOUSE) -> MagicMock:
	"""Return a MagicMock that mimics Wave Settings .get / .get_password."""
	settings = MagicMock(name="WaveSettings")
	values = {
		"outbound_stock_sync_enabled": 1 if enabled else 0,
		"default_warehouse": default_warehouse,
		"wave_api_base_url": DUMMY_BASE_URL,
		"wave_app_id": DUMMY_APP_ID,
		"wave_store_id": DUMMY_STORE_ID,
	}
	settings.get.side_effect = lambda key, default=None: values.get(key, default)
	settings.get_password.return_value = DUMMY_API_KEY
	return settings


def _fake_sle(item_code: str = DUMMY_ITEM, warehouse: str = DUMMY_DEFAULT_WAREHOUSE, name: str = "SLE-0001") -> MagicMock:
	"""Mock a Stock Ledger Entry with just enough surface for the handler to read."""
	sle = MagicMock(spec=["name", "get"])
	sle.name = name
	sle.get.side_effect = lambda key, default=None: {
		"item_code": item_code,
		"warehouse": warehouse,
	}.get(key, default)
	return sle


class TestSleSubmitHandler(FrappeTestCase):
	"""Verify on_sle_submit's filters route to the right log step / enqueue call."""

	def test_eligible_sle_enqueues_job(self):
		"""SLE in default warehouse with sync on must enqueue the worker job."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings()),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(handler, "log_step") as mock_log,
		):
			handler.on_sle_submit(_fake_sle())

		mock_enqueue.assert_called_once()
		_, kwargs = mock_enqueue.call_args
		self.assertEqual(kwargs["job_id"], f"wave-sync:stock:{DUMMY_ITEM}")
		self.assertTrue(kwargs["deduplicate"])
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertEqual(kwargs["item_code"], DUMMY_ITEM)
		self._assert_logged_step(mock_log, handler.STEP_ENQUEUED)

	def test_kill_switch_off_skips_enqueue(self):
		"""When outbound_stock_sync_enabled is off, no job is queued."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings(enabled=False)),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(handler, "log_step") as mock_log,
		):
			handler.on_sle_submit(_fake_sle())

		mock_enqueue.assert_not_called()
		self._assert_logged_step(mock_log, handler.STEP_SKIPPED_DISABLED)

	def test_other_warehouse_skips_enqueue(self):
		"""SLE in a non-default warehouse is logged but not pushed."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings()),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(handler, "log_step") as mock_log,
		):
			handler.on_sle_submit(_fake_sle(warehouse=DUMMY_OTHER_WAREHOUSE))

		mock_enqueue.assert_not_called()
		self._assert_logged_step(mock_log, handler.STEP_SKIPPED_OTHER_WAREHOUSE)

	def test_no_default_warehouse_configured_logs_warning(self):
		"""Empty default_warehouse means we cannot decide; log + skip."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings(default_warehouse="")),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(handler, "log_step") as mock_log,
		):
			handler.on_sle_submit(_fake_sle())

		mock_enqueue.assert_not_called()
		self._assert_logged_step(mock_log, handler.STEP_SKIPPED_NO_WAREHOUSE_CONFIG)

	def test_sle_without_item_code_is_skipped(self):
		"""Defensive guard: bareSLEs (no item_code) cannot be pushed."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings()),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(handler, "log_step") as mock_log,
		):
			handler.on_sle_submit(_fake_sle(item_code=""))

		mock_enqueue.assert_not_called()
		self._assert_logged_step(mock_log, handler.STEP_SKIPPED_NO_ITEM_CODE)

	def _assert_logged_step(self, mock_log: MagicMock, expected_step: str) -> None:
		"""Confirm at least one log_step call carried the expected step value."""
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(expected_step, steps)


class TestStockPusherWorker(FrappeTestCase):
	"""Verify the worker reads Bin qty, posts the right body, and logs every outcome."""

	def test_push_posts_correct_payload_and_logs_success(self):
		"""Happy path: Bin -> wave_client.post_stock_sync called with absolute qty."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings()),
			patch.object(frappe.db, "get_value", return_value=42.0),
			patch.object(stock_pusher.wave_client, "post_stock_sync", return_value={"ok": True}) as mock_post,
			patch.object(stock_pusher, "log_step") as mock_log,
		):
			stock_pusher.push_item_stock(DUMMY_ITEM, "corr-1")

		mock_post.assert_called_once_with(
			base_url=DUMMY_BASE_URL,
			api_key=DUMMY_API_KEY,
			app_id=DUMMY_APP_ID,
			product_id=DUMMY_ITEM,
			store_id=DUMMY_STORE_ID,
			quantity=42,
		)
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(stock_pusher.STEP_PUSH_ATTEMPT, steps)
		self.assertIn(stock_pusher.STEP_PUSH_SUCCESS, steps)

	def test_push_clamps_negative_qty_to_zero(self):
		"""Negative Bin qty (oversold) is pushed as 0 — Wave shouldn't go negative."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings()),
			patch.object(frappe.db, "get_value", return_value=-3.0),
			patch.object(stock_pusher.wave_client, "post_stock_sync", return_value={}) as mock_post,
			patch.object(stock_pusher, "log_step"),
		):
			stock_pusher.push_item_stock(DUMMY_ITEM, "corr-2")

		self.assertEqual(mock_post.call_args.kwargs["quantity"], 0)

	def test_push_logs_error_when_wave_returns_failure(self):
		"""WaveOutboundError from the client is logged at Error level and swallowed."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings()),
			patch.object(frappe.db, "get_value", return_value=10.0),
			patch.object(
				stock_pusher.wave_client,
				"post_stock_sync",
				side_effect=WaveOutboundError("HTTP 500: boom"),
			),
			patch.object(stock_pusher, "log_step") as mock_log,
		):
			stock_pusher.push_item_stock(DUMMY_ITEM, "corr-3")

		failure_calls = [c for c in mock_log.call_args_list if c.kwargs.get("step") == stock_pusher.STEP_PUSH_FAILED]
		self.assertEqual(len(failure_calls), 1)
		self.assertEqual(failure_calls[0].kwargs.get("level"), "Error")
		self.assertIn("HTTP 500", failure_calls[0].kwargs.get("error_message") or "")

	def test_push_aborts_when_settings_disabled_at_run_time(self):
		"""Kill-switch flipped between enqueue and execution -> log abort, no HTTP."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings(enabled=False)),
			patch.object(stock_pusher.wave_client, "post_stock_sync") as mock_post,
			patch.object(stock_pusher, "log_step") as mock_log,
		):
			stock_pusher.push_item_stock(DUMMY_ITEM, "corr-4")

		mock_post.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(stock_pusher.STEP_PUSH_ABORTED_DISABLED, steps)

	def test_push_stamps_batch_id_into_friendly_id(self):
		"""When called with batch_id, every log row carries it as friendly_id for batch-filtering."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings()),
			patch.object(frappe.db, "get_value", return_value=7.0),
			patch.object(stock_pusher.wave_client, "post_stock_sync", return_value={}),
			patch.object(stock_pusher, "log_step") as mock_log,
		):
			stock_pusher.push_item_stock(DUMMY_ITEM, "corr-batch", batch_id="batch-xyz")

		friendly_ids = [c.kwargs.get("friendly_id") for c in mock_log.call_args_list]
		self.assertTrue(friendly_ids)
		self.assertTrue(all(fid == "batch-xyz" for fid in friendly_ids))

	def test_push_swallows_unexpected_exception_and_logs_it(self):
		"""A non-WaveOutboundError raised mid-flight is caught + logged; never propagates."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings()),
			patch.object(frappe.db, "get_value", side_effect=RuntimeError("db gone")),
			patch.object(stock_pusher, "log_step") as mock_log,
		):
			# Must not raise.
			stock_pusher.push_item_stock(DUMMY_ITEM, "corr-boom")

		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(stock_pusher.STEP_PUSH_UNEXPECTED_ERROR, steps)

	def test_push_aborts_on_missing_outbound_config(self):
		"""Empty wave_app_id (or any required outbound field) blocks the call with a clean log."""
		settings = _stub_settings()
		# Override wave_app_id to empty so the resolver returns None.
		original = settings.get.side_effect

		def _missing_app_id(key, default=None):
			if key == "wave_app_id":
				return ""
			return original(key, default)

		settings.get.side_effect = _missing_app_id
		with (
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe.db, "get_value", return_value=5.0),
			patch.object(stock_pusher.wave_client, "post_stock_sync") as mock_post,
			patch.object(stock_pusher, "log_step") as mock_log,
		):
			stock_pusher.push_item_stock(DUMMY_ITEM, "corr-5")

		mock_post.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(stock_pusher.STEP_PUSH_ABORTED_MISSING_CONFIG, steps)


class TestWaveClient(FrappeTestCase):
	"""HTTP-shape tests for wave_client.post_stock_sync."""

	def test_builds_correct_url_headers_and_body(self):
		"""URL, headers, and JSON body match what the n8n reference implementation sends."""
		fake_response = MagicMock(status_code=200, content=b'{"ok":true}')
		fake_response.json.return_value = {"ok": True}

		with patch.object(requests, "post", return_value=fake_response) as mock_post:
			result = wave_client.post_stock_sync(
				base_url=DUMMY_BASE_URL,
				api_key=DUMMY_API_KEY,
				app_id=DUMMY_APP_ID,
				product_id=DUMMY_ITEM,
				store_id=DUMMY_STORE_ID,
				quantity=256,
			)

		self.assertEqual(result, {"ok": True})
		args, kwargs = mock_post.call_args
		self.assertEqual(args[0], f"{DUMMY_BASE_URL}/api/v3/admin/products/{DUMMY_ITEM}/stock/sync")
		self.assertEqual(kwargs["headers"]["X-API-Key"], DUMMY_API_KEY)
		self.assertEqual(kwargs["headers"]["appId"], DUMMY_APP_ID)
		self.assertEqual(kwargs["headers"]["accept"], "application/json")
		self.assertEqual(kwargs["headers"]["content-type"], "application/json")
		self.assertEqual(
			kwargs["json"],
			{"productId": DUMMY_ITEM, "storeId": DUMMY_STORE_ID, "quantity": 256},
		)

	def test_non_2xx_raises_outbound_error_with_response_snippet(self):
		"""HTTP 5xx from Wave bubbles up as WaveOutboundError carrying the body."""
		fake_response = MagicMock(status_code=500)
		fake_response.text = "internal server error"
		fake_response.content = b"internal server error"

		with patch.object(requests, "post", return_value=fake_response):
			with self.assertRaises(WaveOutboundError) as ctx:
				wave_client.post_stock_sync(
					base_url=DUMMY_BASE_URL,
					api_key=DUMMY_API_KEY,
					app_id=DUMMY_APP_ID,
					product_id=DUMMY_ITEM,
					store_id=DUMMY_STORE_ID,
					quantity=1,
				)
		self.assertIn("HTTP 500", str(ctx.exception))
		self.assertIn("internal server error", str(ctx.exception))

	def test_network_error_wrapped_as_outbound_error(self):
		"""requests.RequestException becomes a WaveOutboundError with a clean message."""
		with patch.object(requests, "post", side_effect=requests.ConnectionError("dns fail")):
			with self.assertRaises(WaveOutboundError) as ctx:
				wave_client.post_stock_sync(
					base_url=DUMMY_BASE_URL,
					api_key=DUMMY_API_KEY,
					app_id=DUMMY_APP_ID,
					product_id=DUMMY_ITEM,
					store_id=DUMMY_STORE_ID,
					quantity=1,
				)
		self.assertIn("network error", str(ctx.exception))

	def test_missing_required_input_raises_without_calling_http(self):
		"""Empty product_id is rejected before any HTTP attempt."""
		with patch.object(requests, "post") as mock_post:
			with self.assertRaises(WaveOutboundError):
				wave_client.post_stock_sync(
					base_url=DUMMY_BASE_URL,
					api_key=DUMMY_API_KEY,
					app_id=DUMMY_APP_ID,
					product_id="",
					store_id=DUMMY_STORE_ID,
					quantity=1,
				)
		mock_post.assert_not_called()
