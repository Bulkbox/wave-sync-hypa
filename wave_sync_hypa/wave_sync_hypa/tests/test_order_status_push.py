"""Unit tests for Phase 5 outbound Sales Order status push.

Three layers exercised: the resolver (pure-function rule matching), the
handler (doc_event entry point), and the worker (HTTP call shape + log
emission). All HTTP and most DB lookups are mocked at the boundary so
tests don't side-effect the queue or touch a real Sales Order.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.api import sales_order_status as endpoint
from wave_sync_hypa.wave_sync_hypa.handlers import order_status as handler
from wave_sync_hypa.wave_sync_hypa.services import (
	order_status_pusher,
	order_status_resolver,
	wave_client,
)
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveOutboundError

DUMMY_BASE_URL = "https://wave.example.com"
DUMMY_API_KEY = "outbound-api-key"
DUMMY_APP_ID = "outbound-app-id"
DUMMY_SO = "SO-2026-0001"
DUMMY_WAVE_ORDER_ID = "W-12345"


def _stub_settings(
	*,
	enabled: bool = True,
	rules: list | None = None,
) -> MagicMock:
	"""Return a settings stand-in with the fields the resolver / handler / worker read."""
	settings = MagicMock(name="WaveSettings")
	values = {
		"outbound_order_status_sync_enabled": 1 if enabled else 0,
		"wave_api_base_url": DUMMY_BASE_URL,
		"wave_app_id": DUMMY_APP_ID,
		"outbound_status_rules": rules or [],
	}
	settings.get.side_effect = lambda key, default=None: values.get(key, default)
	settings.get_password.return_value = DUMMY_API_KEY
	settings.outbound_order_status_sync_enabled = 1 if enabled else 0
	return settings


def _rule(**overrides) -> dict:
	"""Build a rule row with sensible defaults; overrides win."""
	row = {
		"enabled": 1,
		"erp_doctype": "Sales Order",
		"erp_event": "submit",
		"erp_condition_field": None,
		"erp_condition_value": None,
		"wave_status": "ACCEPTED",
		"wave_delivery_status": None,
	}
	row.update(overrides)
	return row


def _so_doc(name: str = DUMMY_SO, wave_order_id: str = DUMMY_WAVE_ORDER_ID,
			docstatus: int = 1, **extras) -> SimpleNamespace:
	"""Fabricate a Sales Order stand-in with .doctype, .name, .get(), and arbitrary attributes."""
	doc = SimpleNamespace(doctype="Sales Order", name=name, docstatus=docstatus)
	values = {"wave_order_id": wave_order_id, **extras}
	doc.get = lambda key, default=None: values.get(key, default)
	for k, v in values.items():
		setattr(doc, k, v)
	return doc


class TestResolver(FrappeTestCase):
	"""Pure-function rule matcher; no Frappe / HTTP / DB."""

	def test_returns_none_when_no_rule_matches(self):
		settings = _stub_settings(rules=[_rule(erp_event="cancel")])
		self.assertIsNone(
			order_status_resolver.resolve_outbound_payload(_so_doc(), "submit", settings)
		)

	def test_skips_disabled_rules(self):
		settings = _stub_settings(rules=[_rule(enabled=0)])
		self.assertIsNone(
			order_status_resolver.resolve_outbound_payload(_so_doc(), "submit", settings)
		)

	def test_merges_two_matching_rules_into_one_payload(self):
		"""One rule sets status, another sets deliveryStatus → resolver returns merged dict."""
		settings = _stub_settings(rules=[
			_rule(wave_status="UNDER_DELIVERY", wave_delivery_status=None),
			_rule(wave_status=None, wave_delivery_status="OUT_FOR_DELIVERY"),
		])
		payload = order_status_resolver.resolve_outbound_payload(_so_doc(), "submit", settings)
		self.assertEqual(payload, {"status": "UNDER_DELIVERY", "deliveryStatus": "OUT_FOR_DELIVERY"})

	def test_evaluates_optional_condition_field(self):
		"""Rule with condition only matches when doc.<field> equals configured value."""
		settings = _stub_settings(rules=[
			_rule(
				erp_event="update_after_submit",
				erp_condition_field="delivery_status",
				erp_condition_value="Delivered",
				wave_status=None,
				wave_delivery_status="DELIVERED",
			),
		])
		# Doc with delivery_status=To Deliver → no match.
		self.assertIsNone(
			order_status_resolver.resolve_outbound_payload(
				_so_doc(delivery_status="To Deliver"),
				"update_after_submit",
				settings,
			)
		)
		# Doc with delivery_status=Delivered → match.
		self.assertEqual(
			order_status_resolver.resolve_outbound_payload(
				_so_doc(delivery_status="Delivered"),
				"update_after_submit",
				settings,
			),
			{"deliveryStatus": "DELIVERED"},
		)

	def test_event_must_match_exactly(self):
		settings = _stub_settings(rules=[_rule(erp_event="submit")])
		self.assertIsNone(
			order_status_resolver.resolve_outbound_payload(_so_doc(), "cancel", settings)
		)


class TestHandler(FrappeTestCase):
	"""doc_event entry point: filters and enqueues, never calls HTTP directly."""

	def test_skips_when_kill_switch_off(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings(enabled=False)),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(handler, "log_step") as mock_log,
		):
			handler.on_sales_order_submit(_so_doc())
		mock_enqueue.assert_not_called()
		self.assertIn(
			handler.STEP_SKIPPED_DISABLED,
			[c.kwargs.get("step") for c in mock_log.call_args_list],
		)

	def test_skips_when_no_wave_order_id(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings()),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(handler, "log_step") as mock_log,
		):
			handler.on_sales_order_submit(_so_doc(wave_order_id=""))
		mock_enqueue.assert_not_called()
		self.assertIn(
			handler.STEP_SKIPPED_NO_WAVE_ID,
			[c.kwargs.get("step") for c in mock_log.call_args_list],
		)

	def test_skips_when_no_rule_matches(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_stub_settings(rules=[])),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(handler, "log_step") as mock_log,
		):
			handler.on_sales_order_submit(_so_doc())
		mock_enqueue.assert_not_called()
		self.assertIn(
			handler.STEP_SKIPPED_NO_RULE,
			[c.kwargs.get("step") for c in mock_log.call_args_list],
		)

	def test_enqueues_with_resolved_payload_baked_into_kwargs(self):
		settings = _stub_settings(rules=[_rule(wave_status="ACCEPTED")])
		with (
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(handler, "log_step") as mock_log,
		):
			handler.on_sales_order_submit(_so_doc())
		mock_enqueue.assert_called_once()
		kwargs = mock_enqueue.call_args.kwargs
		self.assertEqual(kwargs["sales_order_name"], DUMMY_SO)
		# Worker function expects `erp_event`, not `event` — `event` is a
		# reserved kwarg in frappe.enqueue's signature and would be eaten
		# before reaching the worker.
		self.assertEqual(kwargs["erp_event"], "submit")
		self.assertNotIn("event", kwargs)
		self.assertEqual(kwargs["payload"], {"status": "ACCEPTED"})
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertIn(
			handler.STEP_ENQUEUED,
			[c.kwargs.get("step") for c in mock_log.call_args_list],
		)


class TestWorker(FrappeTestCase):
	"""Worker job: HTTP shape + log emission + defensive catches."""

	def test_calls_wave_client_and_logs_success(self):
		settings = _stub_settings()
		with (
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe.db, "get_value", return_value=DUMMY_WAVE_ORDER_ID),
			patch.object(order_status_pusher.wave_client, "post_order_status", return_value={"ok": True}) as mock_post,
			patch.object(order_status_pusher, "log_step") as mock_log,
		):
			order_status_pusher.push_order_status(DUMMY_SO, "submit", {"status": "ACCEPTED"}, "corr-1")
		mock_post.assert_called_once_with(
			base_url=DUMMY_BASE_URL,
			api_key=DUMMY_API_KEY,
			app_id=DUMMY_APP_ID,
			order_id=DUMMY_WAVE_ORDER_ID,
			status_name="ACCEPTED",
		)
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(order_status_pusher.STEP_PUSH_ATTEMPT, steps)
		self.assertIn(order_status_pusher.STEP_PUSH_SUCCESS, steps)

	def test_logs_failed_on_outbound_error_and_swallows(self):
		settings = _stub_settings()
		with (
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe.db, "get_value", return_value=DUMMY_WAVE_ORDER_ID),
			patch.object(
				order_status_pusher.wave_client,
				"post_order_status",
				side_effect=WaveOutboundError("HTTP 400: bad status"),
			),
			patch.object(order_status_pusher, "log_step") as mock_log,
		):
			order_status_pusher.push_order_status(DUMMY_SO, "submit", {"status": "ACCEPTED"}, "corr-2")
		failures = [c for c in mock_log.call_args_list if c.kwargs.get("step") == order_status_pusher.STEP_PUSH_FAILED]
		self.assertEqual(len(failures), 1)
		self.assertEqual(failures[0].kwargs.get("level"), "Error")

	def test_soft_skips_terminal_state_when_wave_returns_ORDER0049(self):
		"""ORDER0049 (forbidden transition) -> Warning + STEP_PUSH_SKIPPED_TERMINAL, not Error.

		Drives the amend / re-submit case: ERP fires submit on an SO whose Wave
		counterpart already moved past ACCEPTED. Without this classification
		the ambient Error rate spikes whenever an order is amended, drowning
		real failures in noise.
		"""
		settings = _stub_settings()
		with (
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe.db, "get_value", return_value=DUMMY_WAVE_ORDER_ID),
			patch.object(
				order_status_pusher.wave_client,
				"post_order_status",
				side_effect=WaveOutboundError(
					"Wave order status returned HTTP 422: ...",
					http_status=422,
					wave_code="ORDER0049",
				),
			),
			patch.object(order_status_pusher, "log_step") as mock_log,
		):
			order_status_pusher.push_order_status(DUMMY_SO, "submit", {"status": "ACCEPTED"}, "corr-0049")

		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(order_status_pusher.STEP_PUSH_SKIPPED_TERMINAL, steps)
		self.assertNotIn(order_status_pusher.STEP_PUSH_FAILED, steps)
		skipped = [
			c for c in mock_log.call_args_list
			if c.kwargs.get("step") == order_status_pusher.STEP_PUSH_SKIPPED_TERMINAL
		]
		self.assertEqual(skipped[0].kwargs.get("level"), "Warning")
		# stack_trace is suppressed on soft-skips — terminal-state rejections
		# carry no useful traceback (the failure is on Wave's side).
		self.assertIsNone(skipped[0].kwargs.get("stack_trace"))

	def test_soft_skips_terminal_state_when_wave_returns_ORDER0034(self):
		"""ORDER0034 (unauthorized — order already terminal) is treated identically to ORDER0049."""
		settings = _stub_settings()
		with (
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe.db, "get_value", return_value=DUMMY_WAVE_ORDER_ID),
			patch.object(
				order_status_pusher.wave_client,
				"post_order_status",
				side_effect=WaveOutboundError(
					"Wave order status returned HTTP 422: ...",
					http_status=422,
					wave_code="ORDER0034",
				),
			),
			patch.object(order_status_pusher, "log_step") as mock_log,
		):
			order_status_pusher.push_order_status(DUMMY_SO, "cancel", {"status": "CANCELLED"}, "corr-0034")

		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(order_status_pusher.STEP_PUSH_SKIPPED_TERMINAL, steps)
		self.assertNotIn(order_status_pusher.STEP_PUSH_FAILED, steps)

	def test_unknown_wave_code_still_logged_as_error(self):
		"""A 422 with an unfamiliar wave_code stays Error so the team gets paged."""
		settings = _stub_settings()
		with (
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe.db, "get_value", return_value=DUMMY_WAVE_ORDER_ID),
			patch.object(
				order_status_pusher.wave_client,
				"post_order_status",
				side_effect=WaveOutboundError(
					"Wave order status returned HTTP 422: ...",
					http_status=422,
					wave_code="ORDER9999",
				),
			),
			patch.object(order_status_pusher, "log_step") as mock_log,
		):
			order_status_pusher.push_order_status(DUMMY_SO, "submit", {"status": "ACCEPTED"}, "corr-unknown-422")

		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(order_status_pusher.STEP_PUSH_FAILED, steps)
		self.assertNotIn(order_status_pusher.STEP_PUSH_SKIPPED_TERMINAL, steps)

	def test_aborts_when_kill_switch_off_at_run_time(self):
		settings = _stub_settings(enabled=False)
		with (
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(order_status_pusher.wave_client, "post_order_status") as mock_post,
			patch.object(order_status_pusher, "log_step") as mock_log,
		):
			order_status_pusher.push_order_status(DUMMY_SO, "submit", {"status": "ACCEPTED"}, "corr-3")
		mock_post.assert_not_called()
		self.assertIn(
			order_status_pusher.STEP_PUSH_ABORTED_DISABLED,
			[c.kwargs.get("step") for c in mock_log.call_args_list],
		)

	def test_aborts_when_so_lost_wave_order_id(self):
		settings = _stub_settings()
		with (
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe.db, "get_value", return_value=None),
			patch.object(order_status_pusher.wave_client, "post_order_status") as mock_post,
			patch.object(order_status_pusher, "log_step") as mock_log,
		):
			order_status_pusher.push_order_status(DUMMY_SO, "submit", {"status": "ACCEPTED"}, "corr-4")
		mock_post.assert_not_called()
		self.assertIn(
			order_status_pusher.STEP_PUSH_ABORTED_NO_WAVE_ID,
			[c.kwargs.get("step") for c in mock_log.call_args_list],
		)

	def test_swallows_unexpected_exception_and_logs_it(self):
		with (
			patch.object(frappe, "get_cached_doc", side_effect=RuntimeError("settings dead")),
			patch.object(order_status_pusher, "log_step") as mock_log,
		):
			# Must not raise.
			order_status_pusher.push_order_status(DUMMY_SO, "submit", {"status": "ACCEPTED"}, "corr-5")
		self.assertIn(
			order_status_pusher.STEP_PUSH_UNEXPECTED_ERROR,
			[c.kwargs.get("step") for c in mock_log.call_args_list],
		)

	def test_logs_unsupported_warning_when_payload_carries_only_delivery_status(self):
		"""A rule that sets only wave_delivery_status produces a warning + no HTTP call."""
		settings = _stub_settings()
		with (
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe.db, "get_value", return_value=DUMMY_WAVE_ORDER_ID),
			patch.object(order_status_pusher.wave_client, "post_order_status") as mock_post,
			patch.object(order_status_pusher, "log_step") as mock_log,
		):
			order_status_pusher.push_order_status(
				DUMMY_SO, "submit", {"deliveryStatus": "OUT_FOR_DELIVERY"}, "corr-ds-only"
			)
		mock_post.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(order_status_pusher.STEP_PUSH_DELIVERY_STATUS_UNSUPPORTED, steps)
		self.assertIn(order_status_pusher.STEP_PUSH_ABORTED_EMPTY_PAYLOAD, steps)

	def test_pushes_status_and_warns_when_payload_carries_both(self):
		"""Status + deliveryStatus rule: status is pushed; deliveryStatus emits an unsupported warning."""
		settings = _stub_settings()
		with (
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe.db, "get_value", return_value=DUMMY_WAVE_ORDER_ID),
			patch.object(order_status_pusher.wave_client, "post_order_status", return_value={}) as mock_post,
			patch.object(order_status_pusher, "log_step") as mock_log,
		):
			order_status_pusher.push_order_status(
				DUMMY_SO,
				"submit",
				{"status": "UNDER_DELIVERY", "deliveryStatus": "OUT_FOR_DELIVERY"},
				"corr-both",
			)
		# Status pushed via the path-keyed endpoint.
		mock_post.assert_called_once()
		self.assertEqual(mock_post.call_args.kwargs["status_name"], "UNDER_DELIVERY")
		# Warning emitted for the deliveryStatus side.
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(order_status_pusher.STEP_PUSH_DELIVERY_STATUS_UNSUPPORTED, steps)
		self.assertIn(order_status_pusher.STEP_PUSH_SUCCESS, steps)


class TestWaveClientPostOrderStatus(FrappeTestCase):
	"""HTTP-shape tests for post_order_status (path-keyed POST, no body)."""

	def test_url_path_carries_status_name(self):
		fake_response = MagicMock(status_code=200, content=b'{"ok":true}')
		fake_response.json.return_value = {"ok": True}
		with patch.object(requests, "post", return_value=fake_response) as mock_post:
			result = wave_client.post_order_status(
				base_url=DUMMY_BASE_URL,
				api_key=DUMMY_API_KEY,
				app_id=DUMMY_APP_ID,
				order_id=DUMMY_WAVE_ORDER_ID,
				status_name="ACCEPTED",
			)
		self.assertEqual(result, {"ok": True})
		args, kwargs = mock_post.call_args
		self.assertEqual(
			args[0],
			f"{DUMMY_BASE_URL}/api/v3/admin/orders/{DUMMY_WAVE_ORDER_ID}/status/ACCEPTED",
		)
		self.assertEqual(kwargs["headers"]["X-API-Key"], DUMMY_API_KEY)
		self.assertEqual(kwargs["headers"]["appId"], DUMMY_APP_ID)
		# No body, no content-type header on this endpoint.
		self.assertNotIn("json", kwargs)
		self.assertNotIn("content-type", kwargs["headers"])

	def test_non_2xx_raises_outbound_error(self):
		fake_response = MagicMock(status_code=400)
		fake_response.text = "bad request"
		fake_response.content = b"bad request"
		with patch.object(requests, "post", return_value=fake_response):
			with self.assertRaises(WaveOutboundError) as ctx:
				wave_client.post_order_status(
					base_url=DUMMY_BASE_URL,
					api_key=DUMMY_API_KEY,
					app_id=DUMMY_APP_ID,
					order_id=DUMMY_WAVE_ORDER_ID,
					status_name="WHATEVER",
				)
		self.assertIn("HTTP 400", str(ctx.exception))

	def test_empty_status_name_is_rejected_before_http(self):
		with patch.object(requests, "post") as mock_post:
			with self.assertRaises(WaveOutboundError):
				wave_client.post_order_status(
					base_url=DUMMY_BASE_URL,
					api_key=DUMMY_API_KEY,
					app_id=DUMMY_APP_ID,
					order_id=DUMMY_WAVE_ORDER_ID,
					status_name="",
				)
		mock_post.assert_not_called()


class TestResyncEndpoint(FrappeTestCase):
	"""Manual button: validation + enqueue."""

	def test_refuses_so_without_wave_order_id(self):
		so = _so_doc(wave_order_id="")
		so.check_permission = lambda perm: None
		with (
			patch.object(frappe, "only_for"),
			patch.object(frappe, "get_doc", return_value=so),
		):
			with self.assertRaises(frappe.ValidationError):
				endpoint.resync_order_status(DUMMY_SO)

	def test_refuses_draft_so(self):
		so = _so_doc(docstatus=0)
		so.check_permission = lambda perm: None
		with (
			patch.object(frappe, "only_for"),
			patch.object(frappe, "get_doc", return_value=so),
		):
			with self.assertRaises(frappe.ValidationError):
				endpoint.resync_order_status(DUMMY_SO)

	def test_refuses_when_no_rule_matches(self):
		so = _so_doc(docstatus=1)
		so.check_permission = lambda perm: None
		settings = _stub_settings(rules=[])
		with (
			patch.object(frappe, "only_for"),
			patch.object(frappe, "get_doc", side_effect=[so, settings]),
		):
			with self.assertRaises(frappe.ValidationError):
				endpoint.resync_order_status(DUMMY_SO)

	def test_enqueues_with_event_derived_from_docstatus(self):
		so = _so_doc(docstatus=2)  # cancelled
		so.check_permission = lambda perm: None
		settings = _stub_settings(rules=[
			_rule(erp_event="cancel", wave_status="CANCELLED"),
		])
		with (
			patch.object(frappe, "only_for"),
			patch.object(frappe, "get_doc", side_effect=[so, settings]),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(endpoint, "log_step"),
		):
			result = endpoint.resync_order_status(DUMMY_SO)
		self.assertTrue(result["ok"])
		self.assertEqual(result["event"], "cancel")
		self.assertEqual(result["payload"], {"status": "CANCELLED"})
		# Worker function expects erp_event, not event (frappe.enqueue eats event).
		self.assertEqual(mock_enqueue.call_args.kwargs["erp_event"], "cancel")
		self.assertNotIn("event", mock_enqueue.call_args.kwargs)
