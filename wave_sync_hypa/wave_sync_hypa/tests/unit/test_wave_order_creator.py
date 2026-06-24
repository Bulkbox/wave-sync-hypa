"""Unit tests for services.wave_order_creator.push_so_to_wave.

Orchestrator-level tests: mocks every collaborator (settings, resolver,
builder, wave_client, log_step) and pins the return shape + side effects
(banner flag, Comment, log step) across every branch.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.services import wave_order_creator
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveOutboundError, WaveResolutionError

SO_NAME = "SAL-ORD-2026-99999"
WAVE_ORDER_ID = "wave-order-aaa"
WAVE_FRIENDLY_ID = "10000099"


def _settings(*, push_enabled: int = 1, shop_id: str = "wave-shop-1", full_outbound: bool = True) -> MagicMock:
	values = {
		"enabled": 1,
		"erp_to_wave_push_enabled": push_enabled,
		"wave_shop_id": shop_id,
		"wave_api_base_url": "https://wave.example.com" if full_outbound else "",
		"wave_app_id": "test-app",
		"wave_default_offline_payment_type": "cash",
		"wave_common_offline_customer_id": "wave-cust-default",
		"price_scale_divisor": 100,
	}
	settings = MagicMock(name="WaveSettings")
	settings.get.side_effect = lambda key, default=None: values.get(key, default)
	settings.get_password.return_value = "api-key-123" if full_outbound else ""
	return settings


def _so(wave_order_id: str = "", po_no: str = "") -> MagicMock:
	doc = MagicMock(name="SalesOrderDoc")
	doc.name = SO_NAME
	doc.doctype = "Sales Order"
	doc.wave_order_id = wave_order_id
	doc.po_no = po_no
	doc.get.side_effect = lambda key, default=None: {
		"wave_order_id": wave_order_id,
		"customer": "CUST-001",
		"name": SO_NAME,
	}.get(key, default)
	return doc


class TestPushSoToWavePreconditions(FrappeTestCase):
	"""Settings + state checks short-circuit BEFORE customer/order work."""

	def test_disabled_returns_silently_no_notification(self):
		"""Kill switch off -> {ok: False}, Info log only. No banner/Comment set."""
		so = _so()
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(push_enabled=0)),
			patch.object(frappe, "get_doc", return_value=so),
			patch.object(wave_order_creator, "log_step") as mock_log,
		):
			result = wave_order_creator.push_so_to_wave(SO_NAME, "corr-disabled")
		self.assertFalse(result["ok"])
		so.add_comment.assert_not_called()
		so.db_set.assert_not_called()
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertIn(wave_order_creator.STEP_PUSH_ABORTED_DISABLED, steps)

	def test_already_pushed_returns_silently_no_notification(self):
		"""SO with wave_order_id already set -> abort, no notify."""
		so = _so(wave_order_id="wave-existing")
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "get_doc", return_value=so),
			patch.object(wave_order_creator, "log_step") as mock_log,
		):
			result = wave_order_creator.push_so_to_wave(SO_NAME, "corr-already")
		self.assertFalse(result["ok"])
		self.assertIn("wave-existing", result["reason"])
		so.db_set.assert_not_called()
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertIn(wave_order_creator.STEP_PUSH_ABORTED_ALREADY_PUSHED, steps)

	def test_missing_outbound_config_notifies(self):
		"""No base_url / api_key / app_id -> banner + Comment + Error log."""
		so = _so()
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(full_outbound=False)),
			patch.object(frappe, "get_doc", return_value=so),
			patch.object(wave_order_creator, "log_step") as mock_log,
		):
			result = wave_order_creator.push_so_to_wave(SO_NAME, "corr-noconf")
		self.assertFalse(result["ok"])
		so.db_set.assert_called_once_with("wave_push_failure_required_review", 1, update_modified=False)
		so.add_comment.assert_called_once()
		self.assertIn("Wave push failed", so.add_comment.call_args.args[1])
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertIn(wave_order_creator.STEP_PUSH_ABORTED_MISSING_CONFIG, steps)

	def test_missing_shop_id_notifies(self):
		"""Wave Shop ID blank -> banner + Comment + Error log."""
		so = _so()
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(shop_id="")),
			patch.object(frappe, "get_doc", return_value=so),
			patch.object(wave_order_creator, "log_step") as mock_log,
		):
			result = wave_order_creator.push_so_to_wave(SO_NAME, "corr-noshop")
		self.assertFalse(result["ok"])
		so.db_set.assert_called_once_with("wave_push_failure_required_review", 1, update_modified=False)
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertIn(wave_order_creator.STEP_PUSH_ABORTED_MISSING_SHOP, steps)


class TestPushSoToWaveFailureNotifications(FrappeTestCase):
	"""Resolver / builder / POST failures all surface via the banner + Comment + Error log triad."""

	def test_unresolvable_customer_notifies(self):
		so = _so()
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "get_doc", return_value=so),
			patch.object(
				wave_order_creator.wave_customer_resolver,
				"resolve_wave_customer_for_so",
				side_effect=WaveResolutionError("no customer mapping"),
			),
			patch.object(wave_order_creator, "log_step") as mock_log,
		):
			result = wave_order_creator.push_so_to_wave(SO_NAME, "corr-no-cust")
		self.assertFalse(result["ok"])
		so.db_set.assert_any_call("wave_push_failure_required_review", 1, update_modified=False)
		so.add_comment.assert_called()
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertIn(wave_order_creator.STEP_PUSH_FAILED, steps)

	def test_unresolvable_products_notifies_with_specific_step(self):
		"""Unresolvable products map to STEP_PUSH_ABORTED_UNRESOLVABLE — distinct from generic failure."""
		so = _so()
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "get_doc", return_value=so),
			patch.object(wave_order_creator.wave_customer_resolver, "resolve_wave_customer_for_so", return_value="cust-1"),
			patch.object(
				wave_order_creator.wave_order_builder,
				"build_order_payload",
				side_effect=WaveResolutionError("missing items: ['MISSING-1']"),
			),
			patch.object(wave_order_creator, "log_step") as mock_log,
		):
			result = wave_order_creator.push_so_to_wave(SO_NAME, "corr-no-prod")
		self.assertFalse(result["ok"])
		self.assertIn("MISSING-1", result["reason"])
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertIn(wave_order_creator.STEP_PUSH_ABORTED_UNRESOLVABLE, steps)

	def test_wave_post_http_error_notifies(self):
		so = _so()
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "get_doc", return_value=so),
			patch.object(wave_order_creator.wave_customer_resolver, "resolve_wave_customer_for_so", return_value="cust-1"),
			patch.object(wave_order_creator.wave_order_builder, "build_order_payload", return_value={"products": []}),
			patch.object(
				wave_order_creator.wave_client,
				"create_admin_order",
				side_effect=WaveOutboundError("HTTP 500: server died", http_status=500),
			),
			patch.object(wave_order_creator, "log_step") as mock_log,
		):
			result = wave_order_creator.push_so_to_wave(SO_NAME, "corr-500")
		self.assertFalse(result["ok"])
		self.assertIn("HTTP 500", result["reason"])
		so.db_set.assert_any_call("wave_push_failure_required_review", 1, update_modified=False)
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertIn(wave_order_creator.STEP_PUSH_FAILED, steps)

	def test_wave_response_missing_id_notifies(self):
		"""Wave returned 200/201 but no _id -> treat as failure, notify."""
		so = _so()
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "get_doc", return_value=so),
			patch.object(wave_order_creator.wave_customer_resolver, "resolve_wave_customer_for_so", return_value="cust-1"),
			patch.object(wave_order_creator.wave_order_builder, "build_order_payload", return_value={"products": []}),
			patch.object(wave_order_creator.wave_client, "create_admin_order", return_value={"friendlyId": "x"}),
			patch.object(wave_order_creator, "log_step") as mock_log,
		):
			result = wave_order_creator.push_so_to_wave(SO_NAME, "corr-noid")
		self.assertFalse(result["ok"])
		self.assertIn("_id", result["reason"])
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertIn(wave_order_creator.STEP_PUSH_FAILED, steps)


class TestPushSoToWaveHappyPath(FrappeTestCase):
	"""Successful push stamps wave_order_id + friendly_id + wave_origin; clears banner; logs Success."""

	def test_success_stamps_fields_and_logs_success(self):
		so = _so()
		response = {"_id": WAVE_ORDER_ID, "friendlyId": WAVE_FRIENDLY_ID, "status": "PENDING"}
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "get_doc", return_value=so),
			patch.object(wave_order_creator.wave_customer_resolver, "resolve_wave_customer_for_so", return_value="cust-1"),
			patch.object(wave_order_creator.wave_order_builder, "build_order_payload", return_value={"products": [], "totalPrice": 0}),
			patch.object(wave_order_creator.wave_client, "create_admin_order", return_value=response),
			patch.object(wave_order_creator, "log_step") as mock_log,
		):
			result = wave_order_creator.push_so_to_wave(SO_NAME, "corr-ok")
		self.assertTrue(result["ok"])
		self.assertEqual(result["wave_order_id"], WAVE_ORDER_ID)
		self.assertEqual(result["wave_friendly_id"], WAVE_FRIENDLY_ID)
		# All four stamps fire.
		so.db_set.assert_any_call("wave_order_id", WAVE_ORDER_ID, update_modified=False)
		so.db_set.assert_any_call("wave_friendly_id", WAVE_FRIENDLY_ID, update_modified=False)
		so.db_set.assert_any_call("wave_origin", "ERP Push", update_modified=False)
		so.db_set.assert_any_call("wave_push_failure_required_review", 0, update_modified=False)
		so.add_comment.assert_called()
		self.assertIn(WAVE_ORDER_ID, so.add_comment.call_args.args[1])
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertIn(wave_order_creator.STEP_PUSH_SUCCEEDED, steps)
		self.assertIn(wave_order_creator.STEP_PUSH_ATTEMPT, steps)

	def test_success_stamps_po_no_when_currently_empty(self):
		"""Empty po_no -> stamp it with the Wave friendly id so it shows up in the SO list's PO column."""
		so = _so(po_no="")
		response = {"_id": WAVE_ORDER_ID, "friendlyId": WAVE_FRIENDLY_ID, "status": "PENDING"}
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "get_doc", return_value=so),
			patch.object(wave_order_creator.wave_customer_resolver, "resolve_wave_customer_for_so", return_value="cust-1"),
			patch.object(wave_order_creator.wave_order_builder, "build_order_payload", return_value={"products": [], "totalPrice": 0}),
			patch.object(wave_order_creator.wave_client, "create_admin_order", return_value=response),
			patch.object(wave_order_creator, "log_step"),
		):
			wave_order_creator.push_so_to_wave(SO_NAME, "corr-po")
		so.db_set.assert_any_call("po_no", WAVE_FRIENDLY_ID, update_modified=False)

	def test_success_does_not_overwrite_existing_po_no(self):
		"""An operator-set po_no must survive a successful push — never clobbered."""
		so = _so(po_no="OPERATOR-PO-123")
		response = {"_id": WAVE_ORDER_ID, "friendlyId": WAVE_FRIENDLY_ID, "status": "PENDING"}
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "get_doc", return_value=so),
			patch.object(wave_order_creator.wave_customer_resolver, "resolve_wave_customer_for_so", return_value="cust-1"),
			patch.object(wave_order_creator.wave_order_builder, "build_order_payload", return_value={"products": [], "totalPrice": 0}),
			patch.object(wave_order_creator.wave_client, "create_admin_order", return_value=response),
			patch.object(wave_order_creator, "log_step"),
		):
			wave_order_creator.push_so_to_wave(SO_NAME, "corr-po-keep")
		po_no_calls = [c for c in so.db_set.call_args_list if c.args and c.args[0] == "po_no"]
		self.assertEqual(po_no_calls, [], "po_no must not be touched when already set.")


class TestPushSoToWaveCustomerGate(FrappeTestCase):
	"""A customer flagged ERP -> Wave disabled aborts the create push silently."""

	def test_disabled_customer_aborts_before_http(self):
		so = _so()  # no wave_order_id
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "get_doc", return_value=so),
			patch.object(wave_order_creator.wave_customer_resolver, "is_erp_to_wave_disabled", return_value=True),
			patch.object(wave_order_creator.wave_client, "create_admin_order") as mock_create,
			patch.object(wave_order_creator, "log_step") as mock_log,
		):
			result = wave_order_creator.push_so_to_wave(SO_NAME, "corr-cust-disabled")
		self.assertFalse(result["ok"])
		self.assertIn("ERP → Wave disabled", result["reason"])
		mock_create.assert_not_called()
