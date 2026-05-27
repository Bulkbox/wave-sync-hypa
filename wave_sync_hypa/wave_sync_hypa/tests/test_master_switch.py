"""Unit tests for the master kill switch.

Verifies that each of the 5 chokepoints (1 inbound + 4 outbound workers)
short-circuits with a STEP_MASTER_DISABLED log row and does NOT call the
collaborator below it when Wave Settings.enabled is 0.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.api import (
	pick_list as pl_api,
	sales_order_status as so_status_api,
	wave_settings as ws_api,
)
from wave_sync_hypa.wave_sync_hypa.services import (
	master_switch,
	order_status_pusher,
	pick_list_batch_pusher,
	processor,
	stock_pusher,
	stock_resync,
	wave_order_creator,
)


def _settings(*, enabled: int) -> MagicMock:
	"""Wave Settings stand-in carrying just the master kill switch value."""
	s = MagicMock(name="WaveSettings")
	s.get.side_effect = lambda key, default=None: ({"enabled": enabled}).get(key, default)
	return s


class TestIsWaveIntegrationEnabled(FrappeTestCase):
	"""The helper reads Wave Settings.enabled and casts to bool."""

	def test_returns_true_when_enabled_is_one(self):
		with patch.object(frappe, "get_cached_doc", return_value=_settings(enabled=1)):
			self.assertTrue(master_switch.is_wave_integration_enabled())

	def test_returns_false_when_enabled_is_zero(self):
		with patch.object(frappe, "get_cached_doc", return_value=_settings(enabled=0)):
			self.assertFalse(master_switch.is_wave_integration_enabled())


class TestProcessorRespectsMasterSwitch(FrappeTestCase):
	"""processor.process_webhook is the defence-in-depth inbound chokepoint."""

	def test_short_circuits_with_master_disabled_log_and_no_dispatch(self):
		with (
			patch.object(processor, "is_wave_integration_enabled", return_value=False),
			patch.object(processor, "is_duplicate") as mock_dedup,
			patch.object(processor, "resolve_handler") as mock_resolve,
			patch.object(processor, "log_step") as mock_log,
		):
			processor.process_webhook("corr-A", "ORDER", "CREATE", {"_id": "x"})
		mock_dedup.assert_not_called()
		mock_resolve.assert_not_called()
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertIn(master_switch.STEP_MASTER_DISABLED, steps)


class TestOrderStatusPusherRespectsMasterSwitch(FrappeTestCase):
	"""order_status_pusher.push_order_status: outbound status push worker."""

	def test_short_circuits_before_post(self):
		with (
			patch.object(order_status_pusher, "is_wave_integration_enabled", return_value=False),
			patch.object(order_status_pusher.wave_client, "post_order_status") as mock_post,
			patch.object(order_status_pusher.wave_client, "reject_admin_order") as mock_reject,
			patch.object(order_status_pusher, "log_step") as mock_log,
		):
			order_status_pusher.push_order_status(
				source_doctype="Sales Order",
				source_docname="SO-x",
				erp_event="submit",
				payload={"status": "ACCEPTED"},
				correlation_id="corr-B",
				wave_order_id="wave-x",
			)
		mock_post.assert_not_called()
		mock_reject.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(master_switch.STEP_MASTER_DISABLED, steps)


class TestWaveOrderCreatorRespectsMasterSwitch(FrappeTestCase):
	"""wave_order_creator.push_so_to_wave: ERP->Wave create + manual button."""

	def test_returns_failure_dict_and_no_http(self):
		with (
			patch.object(wave_order_creator, "is_wave_integration_enabled", return_value=False),
			patch.object(wave_order_creator.wave_client, "create_admin_order") as mock_create,
			patch.object(wave_order_creator, "log_step") as mock_log,
		):
			result = wave_order_creator.push_so_to_wave("SO-x", "corr-C")
		self.assertFalse(result["ok"])
		self.assertIn("disabled", result["reason"].lower())
		mock_create.assert_not_called()
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertIn(master_switch.STEP_MASTER_DISABLED, steps)


class TestStockPusherRespectsMasterSwitch(FrappeTestCase):
	"""stock_pusher.push_item_stock: outbound stock sync worker."""

	def test_short_circuits_before_post(self):
		with (
			patch.object(stock_pusher, "is_wave_integration_enabled", return_value=False),
			patch.object(stock_pusher.wave_client, "post_stock_sync") as mock_post,
			patch.object(stock_pusher, "log_step") as mock_log,
		):
			stock_pusher.push_item_stock("JTD011", "corr-D")
		mock_post.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(master_switch.STEP_MASTER_DISABLED, steps)


class TestPickListBatchPusherRespectsMasterSwitch(FrappeTestCase):
	"""pick_list_batch_pusher.push_pick_list_batch_ids: batch IDs PATCH worker."""

	def test_short_circuits_before_patch(self):
		with (
			patch.object(pick_list_batch_pusher, "is_wave_integration_enabled", return_value=False),
			patch.object(pick_list_batch_pusher.wave_client, "patch_order_products") as mock_patch,
			patch.object(pick_list_batch_pusher, "log_step") as mock_log,
		):
			pick_list_batch_pusher.push_pick_list_batch_ids(
				pick_list_name="PL-x",
				wave_order_id="wave-x",
				products_data=[{"item_code": "JTD011", "batch_ids": ["B1"]}],
				correlation_id="corr-E",
				manual_trigger=False,
			)
		mock_patch.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(master_switch.STEP_MASTER_DISABLED, steps)


class TestManualApiEndpointsRefuseWhenMasterOff(FrappeTestCase):
	"""Click-time refusal: 3 manual buttons return {ok: False, reason: ...} when master is off.

	The async-enqueue paths used to silently drop work in the worker — operators
	saw a green "queued" toast. This class pins the synchronous refusal layer so
	the click immediately surfaces an actionable message.
	"""

	def test_push_batch_ids_now_refuses_without_enqueue(self):
		"""api/pick_list.push_batch_ids_now short-circuits when master is off."""
		fake_doc = MagicMock(name="PickListDoc")
		fake_doc.check_permission.return_value = None
		with (
			patch.object(frappe, "get_doc", return_value=fake_doc),
			patch.object(pl_api, "is_wave_integration_enabled", return_value=False),
			patch.object(frappe, "enqueue") as mock_enqueue,
		):
			result = pl_api.push_batch_ids_now("PL-x")
		self.assertFalse(result["ok"])
		self.assertIn("disabled", result["reason"].lower())
		mock_enqueue.assert_not_called()

	def test_resync_order_status_refuses_via_helper_throw(self):
		"""api/sales_order_status._refuse_if_settings_disabled now throws on master off."""
		settings = MagicMock(name="WaveSettings")
		settings.get.side_effect = lambda key, default=None: {"enabled": 0}.get(key, default)
		# outbound_order_status_sync_enabled attribute access — return truthy
		settings.outbound_order_status_sync_enabled = 1
		with self.assertRaises(frappe.ValidationError):
			so_status_api._refuse_if_settings_disabled(settings)

	def test_start_full_resync_refuses_via_helper_throw(self):
		"""api/wave_settings._refuse_if_misconfigured now throws on master off before per-channel check."""
		settings = MagicMock(name="WaveSettings")
		settings.get.side_effect = lambda key, default=None: {"enabled": 0}.get(key, default)
		settings.outbound_stock_sync_enabled = 1
		with self.assertRaises(frappe.ValidationError):
			ws_api._refuse_if_misconfigured(settings)

	def test_stock_resync_coordinator_aborts_when_master_off(self):
		"""services/stock_resync._run_resync defence-in-depth: master off -> STEP_RESYNC_ABORTED, no fan-out."""
		settings = MagicMock(name="WaveSettings")
		settings.get.side_effect = lambda key, default=None: {
			"enabled": 0,
			"outbound_stock_sync_enabled": 1,
			"default_warehouse": "WH-1",
		}.get(key, default)
		with (
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(stock_resync, "log_step") as mock_log,
			patch.object(stock_resync, "_enqueue_each_item") as mock_enqueue_each,
		):
			stock_resync._run_resync("batch-1", item_codes=None)
		mock_enqueue_each.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(stock_resync.STEP_RESYNC_ABORTED, steps)
