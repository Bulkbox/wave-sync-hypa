"""Unit tests for the outbound stock-sync pipeline.

Covers the SLE on_submit handler (filters + enqueue), the stock_pusher worker
(resolve-then-push flow + payload shape + log emission), and the wave_client
HTTP wrapper. All HTTP is mocked at requests.post so tests never touch the
real Wave API.

Phase note: the pusher now resolves a Wave-side product `_id` (cached on
Item.wave_product_id) before posting stock, and re-resolves once when Wave
rejects the cached id with PRODUCT0006. Tests pin both legs of that flow.
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
DUMMY_WAVE_PRODUCT_ID = "69e0d857fe91acfd81c57396"
DUMMY_DEFAULT_WAREHOUSE = "Stores - WAVE"
DUMMY_OTHER_WAREHOUSE = "WIP - WAVE"


def _stub_settings(
	*,
	enabled: bool = True,
	default_warehouse: str = DUMMY_DEFAULT_WAREHOUSE,
	caps_max_quantity: bool = False,
) -> MagicMock:
	"""Return a MagicMock that mimics Wave Settings .get / .get_password."""
	settings = MagicMock(name="WaveSettings")
	values = {
		"enabled": 1,
		"outbound_stock_sync_enabled": 1 if enabled else 0,
		"outbound_stock_caps_max_quantity_enabled": 1 if caps_max_quantity else 0,
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


def _mock_db_get_value(wave_id: str | None = DUMMY_WAVE_PRODUCT_ID, qty: float = 42.0):
	"""Return a side_effect for frappe.db.get_value that handles both pusher call sites.

	The pusher reads two values:
	  - frappe.db.get_value("Item", item_code, "wave_product_id")  -> cached Wave id
	  - frappe.db.get_value("Bin", {...}, "actual_qty")            -> current stock
	One return value can't satisfy both call shapes, so this dispatcher branches
	on the first positional arg (the doctype name).
	"""
	def _impl(*args, **kwargs):
		if not args:
			return None
		if args[0] == "Item":
			return wave_id
		if args[0] == "Bin":
			return qty
		return None
	return _impl


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

	def test_push_uses_cached_wave_id_and_logs_success(self):
		"""Happy path: cached wave_product_id is sent on the URL, body keeps sku in productId."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings()),
			patch.object(frappe.db, "get_value", side_effect=_mock_db_get_value()),
			patch.object(stock_pusher.wave_client, "post_stock_sync", return_value={"ok": True}) as mock_post,
			patch.object(stock_pusher, "log_step") as mock_log,
		):
			stock_pusher.push_item_stock(DUMMY_ITEM, "corr-1")

		mock_post.assert_called_once_with(
			base_url=DUMMY_BASE_URL,
			api_key=DUMMY_API_KEY,
			app_id=DUMMY_APP_ID,
			product_id=DUMMY_WAVE_PRODUCT_ID,
			store_id=DUMMY_STORE_ID,
			quantity=42,
		)
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(stock_pusher.STEP_PUSH_ATTEMPT, steps)
		self.assertIn(stock_pusher.STEP_PUSH_SUCCESS, steps)

	def test_push_resolves_when_no_cached_wave_id(self):
		"""When Item.wave_product_id is empty, the resolver runs once and the resolved id is sent."""
		resolver_id = "newly-resolved-mongo-id"
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings()),
			patch.object(frappe.db, "get_value", side_effect=_mock_db_get_value(wave_id=None)),
			patch.object(
				stock_pusher.product_resolver,
				"resolve_wave_product_id",
				return_value=resolver_id,
			) as mock_resolve,
			patch.object(stock_pusher.wave_client, "post_stock_sync", return_value={"ok": True}) as mock_post,
			patch.object(stock_pusher, "log_step"),
		):
			stock_pusher.push_item_stock(DUMMY_ITEM, "corr-noid")

		mock_resolve.assert_called_once()
		self.assertEqual(mock_post.call_args.kwargs["product_id"], resolver_id)

	def test_push_aborts_when_resolver_returns_none(self):
		"""Item with no cached id and no Wave hit -> log abort, never call HTTP."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings()),
			patch.object(frappe.db, "get_value", side_effect=_mock_db_get_value(wave_id=None)),
			patch.object(
				stock_pusher.product_resolver, "resolve_wave_product_id", return_value=None
			),
			patch.object(stock_pusher.wave_client, "post_stock_sync") as mock_post,
			patch.object(stock_pusher, "log_step") as mock_log,
		):
			stock_pusher.push_item_stock(DUMMY_ITEM, "corr-unmapped")

		mock_post.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(stock_pusher.STEP_PUSH_ABORTED_UNMAPPED, steps)

	def test_push_retries_after_resolve_on_PRODUCT0006(self):
		"""PRODUCT0006 from Wave -> re-resolve once and retry the push with the fresh id."""
		fresh_id = "fresh-wave-id"
		first_error = WaveOutboundError(
			"Wave stock/sync returned HTTP 422: ...",
			http_status=422,
			wave_code="PRODUCT0006",
			response_text="...",
		)
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings()),
			patch.object(frappe.db, "get_value", side_effect=_mock_db_get_value()),
			patch.object(
				stock_pusher.product_resolver,
				"resolve_wave_product_id",
				return_value=fresh_id,
			) as mock_resolve,
			patch.object(
				stock_pusher.wave_client,
				"post_stock_sync",
				side_effect=[first_error, {"ok": True}],
			) as mock_post,
			patch.object(stock_pusher, "log_step") as mock_log,
		):
			stock_pusher.push_item_stock(DUMMY_ITEM, "corr-stale")

		# First call used the cached id, second used the freshly-resolved id.
		first_call_id = mock_post.call_args_list[0].kwargs["product_id"]
		second_call_id = mock_post.call_args_list[1].kwargs["product_id"]
		self.assertEqual(first_call_id, DUMMY_WAVE_PRODUCT_ID)
		self.assertEqual(second_call_id, fresh_id)
		mock_resolve.assert_called_once()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(stock_pusher.STEP_PUSH_RETRY_AFTER_RESOLVE, steps)
		self.assertIn(stock_pusher.STEP_PUSH_SUCCESS, steps)

	def test_push_does_not_retry_on_unrelated_wave_error(self):
		"""Errors other than PRODUCT0006 (auth, 5xx, validation) must not trigger re-resolve."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings()),
			patch.object(frappe.db, "get_value", side_effect=_mock_db_get_value()),
			patch.object(
				stock_pusher.product_resolver, "resolve_wave_product_id"
			) as mock_resolve,
			patch.object(
				stock_pusher.wave_client,
				"post_stock_sync",
				side_effect=WaveOutboundError(
					"Wave stock/sync returned HTTP 500: boom",
					http_status=500,
					wave_code=None,
				),
			) as mock_post,
			patch.object(stock_pusher, "log_step") as mock_log,
		):
			stock_pusher.push_item_stock(DUMMY_ITEM, "corr-500")

		# Exactly one POST attempt, no resolver call.
		self.assertEqual(mock_post.call_count, 1)
		mock_resolve.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(stock_pusher.STEP_PUSH_FAILED, steps)
		self.assertNotIn(stock_pusher.STEP_PUSH_RETRY_AFTER_RESOLVE, steps)

	def test_push_clamps_negative_qty_to_zero(self):
		"""Negative Bin qty (oversold) is pushed as 0 — Wave shouldn't go negative."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings()),
			patch.object(frappe.db, "get_value", side_effect=_mock_db_get_value(qty=-3.0)),
			patch.object(stock_pusher.wave_client, "post_stock_sync", return_value={}) as mock_post,
			patch.object(stock_pusher, "log_step"),
		):
			stock_pusher.push_item_stock(DUMMY_ITEM, "corr-2")

		self.assertEqual(mock_post.call_args.kwargs["quantity"], 0)

	def test_push_logs_error_when_wave_returns_failure(self):
		"""WaveOutboundError from the client is logged at Error level and swallowed."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings()),
			patch.object(frappe.db, "get_value", side_effect=_mock_db_get_value(qty=10.0)),
			patch.object(
				stock_pusher.wave_client,
				"post_stock_sync",
				side_effect=WaveOutboundError("HTTP 500: boom", http_status=500),
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
			patch.object(frappe.db, "get_value", side_effect=_mock_db_get_value(qty=7.0)),
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
			patch.object(frappe.db, "get_value", side_effect=_mock_db_get_value(qty=5.0)),
			patch.object(stock_pusher.wave_client, "post_stock_sync") as mock_post,
			patch.object(stock_pusher, "log_step") as mock_log,
		):
			stock_pusher.push_item_stock(DUMMY_ITEM, "corr-5")

		mock_post.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(stock_pusher.STEP_PUSH_ABORTED_MISSING_CONFIG, steps)

	def test_caps_off_does_not_patch_product(self):
		"""Cap setting off (today's behaviour): stock POST fires, product PATCH does not."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings(caps_max_quantity=False)),
			patch.object(frappe.db, "get_value", side_effect=_mock_db_get_value()),
			patch.object(stock_pusher.wave_client, "post_stock_sync", return_value={"ok": True}) as mock_post,
			patch.object(stock_pusher.wave_client, "patch_product") as mock_patch,
			patch.object(stock_pusher, "log_step"),
		):
			stock_pusher.push_item_stock(DUMMY_ITEM, "corr-caps-off")
		mock_post.assert_called_once()
		mock_patch.assert_not_called()

	def test_caps_on_mirrors_quantity_to_product(self):
		"""Cap setting on + stock POST succeeds -> PATCH body = {'quantityLimit': <same qty>}."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings(caps_max_quantity=True)),
			patch.object(frappe.db, "get_value", side_effect=_mock_db_get_value(qty=42.0)),
			patch.object(stock_pusher.wave_client, "post_stock_sync", return_value={"ok": True}),
			patch.object(stock_pusher.wave_client, "patch_product", return_value={"ok": True}) as mock_patch,
			patch.object(stock_pusher, "log_step") as mock_log,
		):
			stock_pusher.push_item_stock(DUMMY_ITEM, "corr-caps-on")
		mock_patch.assert_called_once_with(
			base_url=DUMMY_BASE_URL,
			api_key=DUMMY_API_KEY,
			app_id=DUMMY_APP_ID,
			product_id=DUMMY_WAVE_PRODUCT_ID,
			body={"quantityLimit": 42},
		)
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(stock_pusher.STEP_QUANTITY_LIMIT_ATTEMPT, steps)
		self.assertIn(stock_pusher.STEP_QUANTITY_LIMIT_PUSHED, steps)

	def test_caps_on_skips_patch_when_stock_push_fails(self):
		"""Stock POST failure short-circuits — PATCH never fires."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings(caps_max_quantity=True)),
			patch.object(frappe.db, "get_value", side_effect=_mock_db_get_value(qty=42.0)),
			patch.object(
				stock_pusher.wave_client,
				"post_stock_sync",
				side_effect=WaveOutboundError("HTTP 500: boom", http_status=500),
			),
			patch.object(stock_pusher.wave_client, "patch_product") as mock_patch,
			patch.object(stock_pusher, "log_step"),
		):
			stock_pusher.push_item_stock(DUMMY_ITEM, "corr-caps-stock-fail")
		mock_patch.assert_not_called()

	def test_caps_on_patch_failure_logs_warning_does_not_raise(self):
		"""Stock POST succeeded -> partial success. PATCH failure logs Warning, no exception."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings(caps_max_quantity=True)),
			patch.object(frappe.db, "get_value", side_effect=_mock_db_get_value(qty=42.0)),
			patch.object(stock_pusher.wave_client, "post_stock_sync", return_value={"ok": True}),
			patch.object(
				stock_pusher.wave_client,
				"patch_product",
				side_effect=WaveOutboundError("HTTP 422: invalid", http_status=422),
			),
			patch.object(stock_pusher, "log_step") as mock_log,
		):
			# Must not raise.
			stock_pusher.push_item_stock(DUMMY_ITEM, "corr-caps-patch-fail")
		failure_calls = [c for c in mock_log.call_args_list if c.kwargs.get("step") == stock_pusher.STEP_QUANTITY_LIMIT_FAILED]
		self.assertEqual(len(failure_calls), 1)
		self.assertEqual(failure_calls[0].kwargs.get("level"), "Warning")
		# Stock push success row still logged — partial success.
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(stock_pusher.STEP_PUSH_SUCCESS, steps)
		self.assertNotIn(stock_pusher.STEP_QUANTITY_LIMIT_PUSHED, steps)

	def test_caps_on_with_zero_quantity_sends_zero(self):
		"""Stock = 0 -> quantityLimit = 0 (mirror exactly; Wave caps order to 0)."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings(caps_max_quantity=True)),
			patch.object(frappe.db, "get_value", side_effect=_mock_db_get_value(qty=0.0)),
			patch.object(stock_pusher.wave_client, "post_stock_sync", return_value={"ok": True}),
			patch.object(stock_pusher.wave_client, "patch_product", return_value={"ok": True}) as mock_patch,
			patch.object(stock_pusher, "log_step"),
		):
			stock_pusher.push_item_stock(DUMMY_ITEM, "corr-caps-zero")
		self.assertEqual(mock_patch.call_args.kwargs["body"], {"quantityLimit": 0})

	def test_caps_on_after_product0006_retry_uses_fresh_id(self):
		"""PRODUCT0006 -> re-resolve -> retry stock POST -> on retry success, PATCH uses fresh id."""
		fresh_id = "fresh-wave-id"
		first_error = WaveOutboundError(
			"Wave stock/sync returned HTTP 422: ...",
			http_status=422,
			wave_code="PRODUCT0006",
		)
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings(caps_max_quantity=True)),
			patch.object(frappe.db, "get_value", side_effect=_mock_db_get_value()),
			patch.object(stock_pusher.product_resolver, "resolve_wave_product_id", return_value=fresh_id),
			patch.object(
				stock_pusher.wave_client,
				"post_stock_sync",
				side_effect=[first_error, {"ok": True}],
			),
			patch.object(stock_pusher.wave_client, "patch_product", return_value={"ok": True}) as mock_patch,
			patch.object(stock_pusher, "log_step"),
		):
			stock_pusher.push_item_stock(DUMMY_ITEM, "corr-caps-retry")
		# Exactly one PATCH call, with the refreshed wave_product_id.
		mock_patch.assert_called_once()
		self.assertEqual(mock_patch.call_args.kwargs["product_id"], fresh_id)


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
		fake_response.json.side_effect = ValueError("not json")

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
		self.assertEqual(ctx.exception.http_status, 500)
		self.assertIsNone(ctx.exception.wave_code)

	def test_non_2xx_with_wave_code_envelope_attaches_code(self):
		"""Wave's standard {code, userMessage, ...} 422 body populates the structured fields."""
		envelope = {
			"code": "PRODUCT0006",
			"userTitle": "Validation Error",
			"userMessage": "Cannot create or update product, the product with id not found.",
		}
		fake_response = MagicMock(status_code=422)
		fake_response.text = '{"code":"PRODUCT0006",...}'
		fake_response.content = b'{"code":"PRODUCT0006",...}'
		fake_response.json.return_value = envelope

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

		self.assertEqual(ctx.exception.http_status, 422)
		self.assertEqual(ctx.exception.wave_code, "PRODUCT0006")

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


class TestWaveClientGetProductBySku(FrappeTestCase):
	"""HTTP-shape tests for wave_client.get_product_by_sku.

	Wave's contract for this endpoint is unusual (200 + empty body for unknown
	sku, not 404), so the wrapper centralises the empty-body -> None mapping
	and these tests pin the behaviour we rely on in product_resolver.
	"""

	def test_returns_parsed_dict_on_200_with_id(self):
		"""200 + JSON body containing _id -> caller gets the parsed dict."""
		body = {"_id": DUMMY_WAVE_PRODUCT_ID, "sku": DUMMY_ITEM, "name": "Test Product"}
		fake = MagicMock(status_code=200, content=b'{"_id":"x"}')
		fake.json.return_value = body

		with patch.object(requests, "get", return_value=fake) as mock_get:
			result = wave_client.get_product_by_sku(
				base_url=DUMMY_BASE_URL,
				api_key=DUMMY_API_KEY,
				app_id=DUMMY_APP_ID,
				sku=DUMMY_ITEM,
			)

		self.assertEqual(result, body)
		args, kwargs = mock_get.call_args
		self.assertEqual(args[0], f"{DUMMY_BASE_URL}/api/v3/products/by-sku/{DUMMY_ITEM}")
		self.assertEqual(kwargs["headers"]["X-API-Key"], DUMMY_API_KEY)
		self.assertEqual(kwargs["headers"]["appId"], DUMMY_APP_ID)

	def test_returns_none_on_200_empty_body(self):
		"""200 with empty content (Wave's not-found convention) -> None, no exception."""
		fake = MagicMock(status_code=200, content=b"")
		fake.text = ""
		with patch.object(requests, "get", return_value=fake):
			result = wave_client.get_product_by_sku(
				base_url=DUMMY_BASE_URL,
				api_key=DUMMY_API_KEY,
				app_id=DUMMY_APP_ID,
				sku="DOES-NOT-EXIST",
			)
		self.assertIsNone(result)

	def test_returns_none_on_200_body_without_id(self):
		"""Defensive: 200 with JSON that has no _id is treated identically to empty body."""
		fake = MagicMock(status_code=200, content=b'{"oops":"no id here"}')
		fake.json.return_value = {"oops": "no id here"}
		with patch.object(requests, "get", return_value=fake):
			result = wave_client.get_product_by_sku(
				base_url=DUMMY_BASE_URL,
				api_key=DUMMY_API_KEY,
				app_id=DUMMY_APP_ID,
				sku="STRANGE",
			)
		self.assertIsNone(result)

	def test_5xx_raises_outbound_error(self):
		"""Real HTTP errors (auth, 5xx) still raise — caller distinguishes them from not-found."""
		fake = MagicMock(status_code=503, text="upstream broken", content=b"upstream broken")
		fake.json.side_effect = ValueError("not json")
		with patch.object(requests, "get", return_value=fake):
			with self.assertRaises(WaveOutboundError) as ctx:
				wave_client.get_product_by_sku(
					base_url=DUMMY_BASE_URL,
					api_key=DUMMY_API_KEY,
					app_id=DUMMY_APP_ID,
					sku=DUMMY_ITEM,
				)
		self.assertEqual(ctx.exception.http_status, 503)

	def test_empty_sku_rejected_without_http_call(self):
		"""Empty sku is rejected client-side before any network attempt."""
		with patch.object(requests, "get") as mock_get:
			with self.assertRaises(WaveOutboundError):
				wave_client.get_product_by_sku(
					base_url=DUMMY_BASE_URL,
					api_key=DUMMY_API_KEY,
					app_id=DUMMY_APP_ID,
					sku="",
				)
		mock_get.assert_not_called()
