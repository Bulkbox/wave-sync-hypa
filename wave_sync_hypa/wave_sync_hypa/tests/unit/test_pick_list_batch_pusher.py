"""Unit tests for services.pick_list_batch_pusher.push_pick_list_batch_ids.

Worker-side tests: kill-switch checks, item resolution, payload shaping,
HTTP outcome logging, defensive try/except. wave_client and product_resolver
are patched at the module boundary so no real HTTP fires.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.services import pick_list_batch_pusher
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveOutboundError

DUMMY_BASE_URL = "https://wave.example.com"
DUMMY_API_KEY = "outbound-api-key"
DUMMY_APP_ID = "outbound-app-id"
DUMMY_PL = "PICK-2026-0001"
DUMMY_WAVE_ORDER_ID = "wave-id-aaa"


def _settings(*, batch_ids_enabled: int = 1, full_config: bool = True) -> MagicMock:
	"""Wave Settings stand-in: kill-switch + outbound HTTP config."""
	values = {
		"enabled": 1,
		"pick_list_batch_ids_push_enabled": batch_ids_enabled,
		"wave_api_base_url": DUMMY_BASE_URL if full_config else "",
		"wave_app_id": DUMMY_APP_ID if full_config else "",
	}
	settings = MagicMock(name="WaveSettings")
	settings.get.side_effect = lambda key, default=None: values.get(key, default)
	settings.get_password.return_value = DUMMY_API_KEY if full_config else ""
	return settings


def _call(products_data: list[dict], correlation_id: str = "corr-1") -> None:
	pick_list_batch_pusher.push_pick_list_batch_ids(
		pick_list_name=DUMMY_PL,
		wave_order_id=DUMMY_WAVE_ORDER_ID,
		products_data=products_data,
		correlation_id=correlation_id,
	)


class TestPushPickListBatchIds(FrappeTestCase):
	"""Worker job: validate config, resolve products, PATCH, log every transition."""

	def test_aborts_when_kill_switch_off_at_runtime(self):
		"""Settings.pick_list_batch_ids_push_enabled flipped off mid-queue -> Warning + no PATCH."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(batch_ids_enabled=0)),
			patch.object(pick_list_batch_pusher.wave_client, "patch_order_products") as mock_patch,
			patch.object(pick_list_batch_pusher, "log_step") as mock_log,
		):
			_call([{"item_code": "JTD011", "batch_ids": ["B-001"]}])
		mock_patch.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pick_list_batch_pusher.STEP_ABORTED_DISABLED, steps)

	def test_manual_trigger_bypasses_kill_switch(self):
		"""manual_trigger=True PATCHes even when pick_list_batch_ids_push_enabled is off."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(batch_ids_enabled=0)),
			patch.object(frappe.db, "get_value", return_value="wave-prod-jtd011"),
			patch.object(pick_list_batch_pusher.wave_client, "patch_order_products", return_value={"_id": DUMMY_WAVE_ORDER_ID}) as mock_patch,
			patch.object(pick_list_batch_pusher, "log_step") as mock_log,
		):
			pick_list_batch_pusher.push_pick_list_batch_ids(
				pick_list_name=DUMMY_PL,
				wave_order_id=DUMMY_WAVE_ORDER_ID,
				products_data=[{"item_code": "JTD011", "batch_ids": ["B-001"]}],
				correlation_id="corr-manual",
				manual_trigger=True,
			)
		mock_patch.assert_called_once()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertNotIn(pick_list_batch_pusher.STEP_ABORTED_DISABLED, steps)
		self.assertIn(pick_list_batch_pusher.STEP_PUSH_SUCCESS, steps)

	def test_aborts_when_outbound_config_incomplete(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(full_config=False)),
			patch.object(pick_list_batch_pusher.wave_client, "patch_order_products") as mock_patch,
			patch.object(pick_list_batch_pusher, "log_step") as mock_log,
		):
			_call([{"item_code": "JTD011", "batch_ids": ["B-001"]}])
		mock_patch.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pick_list_batch_pusher.STEP_ABORTED_MISSING_CONFIG, steps)

	def test_aborts_when_wave_order_id_empty(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(pick_list_batch_pusher.wave_client, "patch_order_products") as mock_patch,
			patch.object(pick_list_batch_pusher, "log_step") as mock_log,
		):
			pick_list_batch_pusher.push_pick_list_batch_ids(
				pick_list_name=DUMMY_PL,
				wave_order_id="",
				products_data=[{"item_code": "JTD011", "batch_ids": ["B-001"]}],
				correlation_id="corr-empty",
			)
		mock_patch.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pick_list_batch_pusher.STEP_ABORTED_NO_WAVE_ID, steps)

	def test_skips_items_that_cannot_resolve_to_wave_product_id(self):
		"""Unresolved item -> Warning row, item dropped from body, others still pushed."""
		def _resolver_side_effect(*args, **kwargs):
			return {"JTD011": "wave-prod-jtd011", "MISSING": None}.get(args[0])

		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe.db, "get_value", return_value=None),  # no cached wave_product_id
			patch.object(
				pick_list_batch_pusher.product_resolver,
				"resolve_wave_product_id",
				side_effect=_resolver_side_effect,
			),
			patch.object(pick_list_batch_pusher.wave_client, "patch_order_products", return_value={"_id": DUMMY_WAVE_ORDER_ID}) as mock_patch,
			patch.object(pick_list_batch_pusher, "log_step") as mock_log,
		):
			_call([
				{"item_code": "JTD011", "batch_ids": ["B-001"]},
				{"item_code": "MISSING", "batch_ids": ["B-X"]},
			])

		# PATCH was issued with only the resolved item; raw-array body shape.
		mock_patch.assert_called_once()
		body = mock_patch.call_args.kwargs["body"]
		self.assertEqual(body, [{"productId": "wave-prod-jtd011", "batchIds": ["B-001"]}])
		# Skip row was logged.
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pick_list_batch_pusher.STEP_SKIPPED_UNRESOLVED_ITEM, steps)

	def test_aborts_when_all_items_skipped(self):
		"""Every item unresolved -> empty body -> aborted_empty_payload, no PATCH."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe.db, "get_value", return_value=None),
			patch.object(
				pick_list_batch_pusher.product_resolver,
				"resolve_wave_product_id",
				return_value=None,
			),
			patch.object(pick_list_batch_pusher.wave_client, "patch_order_products") as mock_patch,
			patch.object(pick_list_batch_pusher, "log_step") as mock_log,
		):
			_call([{"item_code": "MISSING", "batch_ids": ["B-X"]}])
		mock_patch.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pick_list_batch_pusher.STEP_ABORTED_EMPTY_PAYLOAD, steps)

	def test_calls_patch_order_products_with_raw_array_body(self):
		"""Body is a raw array (no wrapper) and each entry only has productId + batchIds."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe.db, "get_value", return_value="wave-prod-jtd011"),  # cached id hit
			patch.object(pick_list_batch_pusher.wave_client, "patch_order_products", return_value={"_id": DUMMY_WAVE_ORDER_ID, "status": "ACCEPTED"}) as mock_patch,
			patch.object(pick_list_batch_pusher, "log_step") as mock_log,
		):
			_call([{"item_code": "JTD011", "batch_ids": ["B-001", "B-002"]}])

		mock_patch.assert_called_once_with(
			base_url=DUMMY_BASE_URL,
			api_key=DUMMY_API_KEY,
			app_id=DUMMY_APP_ID,
			order_id=DUMMY_WAVE_ORDER_ID,
			body=[{"productId": "wave-prod-jtd011", "batchIds": ["B-001", "B-002"]}],
		)
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pick_list_batch_pusher.STEP_PUSH_ATTEMPT, steps)
		self.assertIn(pick_list_batch_pusher.STEP_PUSH_SUCCESS, steps)

	def test_dedupes_batch_ids_per_item(self):
		"""Repeated batch_ids in the input collapse to distinct values, encounter order."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe.db, "get_value", return_value="wave-prod-jtd011"),
			patch.object(pick_list_batch_pusher.wave_client, "patch_order_products", return_value={}) as mock_patch,
			patch.object(pick_list_batch_pusher, "log_step"),
		):
			_call([{"item_code": "JTD011", "batch_ids": ["B-001", "B-002", "B-001", "B-003"]}])
		body = mock_patch.call_args.kwargs["body"]
		self.assertEqual(body[0]["batchIds"], ["B-001", "B-002", "B-003"])

	def test_forwards_comments_to_patch_body_when_present(self):
		"""products_data entry with 'comments' -> the string lands on the final PATCH body entry."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe.db, "get_value", return_value="wave-prod-jtd011"),
			patch.object(pick_list_batch_pusher.wave_client, "patch_order_products", return_value={}) as mock_patch,
			patch.object(pick_list_batch_pusher, "log_step"),
		):
			_call([{
				"item_code": "JTD011",
				"batch_ids": ["B-001", "B-002"],
				"comments": "- B-001: 3\n- B-002: 2",
			}])
		body = mock_patch.call_args.kwargs["body"]
		self.assertEqual(
			body,
			[{
				"productId": "wave-prod-jtd011",
				"batchIds": ["B-001", "B-002"],
				"comments": "- B-001: 3\n- B-002: 2",
			}],
		)

	def test_omits_comments_when_blank_or_missing(self):
		"""Empty / missing comments field does not appear in the PATCH body — keeps body minimal."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe.db, "get_value", return_value="wave-prod-jtd011"),
			patch.object(pick_list_batch_pusher.wave_client, "patch_order_products", return_value={}) as mock_patch,
			patch.object(pick_list_batch_pusher, "log_step"),
		):
			_call([{"item_code": "JTD011", "batch_ids": ["B-001"], "comments": "   "}])
		body = mock_patch.call_args.kwargs["body"]
		self.assertNotIn("comments", body[0])

	def test_logs_failed_on_outbound_error_and_swallows(self):
		"""WaveOutboundError -> Failed Error row, no exception raised out of worker."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe.db, "get_value", return_value="wave-prod-jtd011"),
			patch.object(
				pick_list_batch_pusher.wave_client,
				"patch_order_products",
				side_effect=WaveOutboundError("HTTP 500: server error"),
			),
			patch.object(pick_list_batch_pusher, "log_step") as mock_log,
		):
			_call([{"item_code": "JTD011", "batch_ids": ["B-001"]}])
		failed = [
			c for c in mock_log.call_args_list
			if c.kwargs.get("step") == pick_list_batch_pusher.STEP_PUSH_FAILED
		]
		self.assertEqual(len(failed), 1)
		self.assertEqual(failed[0].kwargs.get("level"), "Error")

	def test_logs_unexpected_error_and_does_not_raise(self):
		with (
			patch.object(frappe, "get_cached_doc", side_effect=RuntimeError("settings dead")),
			patch.object(pick_list_batch_pusher, "log_step") as mock_log,
		):
			# Must not raise.
			_call([{"item_code": "JTD011", "batch_ids": ["B-001"]}])
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pick_list_batch_pusher.STEP_UNEXPECTED_ERROR, steps)
