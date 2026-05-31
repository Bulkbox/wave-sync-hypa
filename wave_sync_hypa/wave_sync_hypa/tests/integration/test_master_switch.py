"""Unit tests for the master kill switch.

Two altitudes are covered:

  * Worker / API backstops — each outbound worker (and the inbound processor)
    short-circuits with a STEP_MASTER_DISABLED log row and does NOT call the
    collaborator below it when Wave Settings.enabled is 0.
  * Decision-layer guards — the ERP-side doc_event handlers and the dispatch
    fan-out short-circuit via skip_if_disabled() so that with the switch off
    NO background job is enqueued at all (not merely dropped by the worker).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.api import (
	pick_list as pl_api,
)
from wave_sync_hypa.wave_sync_hypa.api import (
	sales_order_status as so_status_api,
)
from wave_sync_hypa.wave_sync_hypa.api import (
	shipday_intercept,
)
from wave_sync_hypa.wave_sync_hypa.api import (
	wave_settings as ws_api,
)
from wave_sync_hypa.wave_sync_hypa.handlers import (
	order_status,
	payment_entry,
	stock_sync,
)
from wave_sync_hypa.wave_sync_hypa.handlers import (
	pick_list as pl_handler,
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


class TestDecisionLayerSkipsEnqueueWhenMasterOff(FrappeTestCase):
	"""skip_if_disabled() short-circuits the ERP-side handlers BEFORE they enqueue.

	With the master switch off these paths must queue NO background job (the
	old behaviour queued a job the worker then dropped) and must write a
	STEP_MASTER_DISABLED audit row via the shared guard.
	"""

	def _disabled(self):
		"""Context managers common to every decision-layer test: switch off, no DB, no queue."""
		return (
			patch.object(master_switch, "is_wave_integration_enabled", return_value=False),
			patch.object(master_switch, "log_step"),
			patch.object(frappe, "get_cached_doc", return_value=MagicMock()),
			patch.object(frappe, "enqueue"),
		)

	@staticmethod
	def _logged_master_disabled(mock_log) -> bool:
		# skip_if_disabled calls log_step(correlation_id, STEP_MASTER_DISABLED, "Info", ...)
		return any(
			len(c.args) > 1 and c.args[1] == master_switch.STEP_MASTER_DISABLED
			for c in mock_log.call_args_list
		)

	def test_dispatch_with_wave_order_ids_skips(self):
		"""The fan-out choke point (SO/DN/SI/Shipday/Pick-List status) queues nothing when off."""
		doc = MagicMock()
		doc.doctype = "Sales Order"
		doc.name = "SO-x"
		sw, log, cached, enqueue = self._disabled()
		with (
			sw,
			log as mock_log,
			cached,
			enqueue as mock_enqueue,
			patch.object(order_status.order_status_resolver, "resolve_outbound_payload") as mock_resolve,
		):
			order_status.dispatch_with_wave_order_ids(doc, "submit", ["wave-x"])
		mock_enqueue.assert_not_called()
		mock_resolve.assert_not_called()
		self.assertTrue(self._logged_master_disabled(mock_log))

	def test_maybe_auto_push_skips_before_settings_read(self):
		"""ERP -> Wave auto-push guard is the first statement: no settings read, no enqueue."""
		doc = MagicMock()
		doc.doctype = "Sales Order"
		doc.name = "SO-x"
		sw, log, cached, enqueue = self._disabled()
		with sw, log as mock_log, cached as mock_cached, enqueue as mock_enqueue:
			order_status.maybe_auto_push_to_wave(doc)
		mock_enqueue.assert_not_called()
		mock_cached.assert_not_called()
		self.assertTrue(self._logged_master_disabled(mock_log))

	def test_on_sle_submit_skips(self):
		"""Stock Ledger Entry submit queues no stock push when off."""
		doc = MagicMock()
		doc.name = "SLE-x"
		doc.get.side_effect = lambda k, default=None: {"item_code": "JTD011", "warehouse": "WH-1"}.get(
			k, default
		)
		sw, log, cached, enqueue = self._disabled()
		with sw, log as mock_log, cached, enqueue as mock_enqueue:
			stock_sync.on_sle_submit(doc)
		mock_enqueue.assert_not_called()
		self.assertTrue(self._logged_master_disabled(mock_log))

	def test_on_payment_entry_submit_skips(self):
		"""Payment Entry submit resolves nothing and queues nothing when off."""
		doc = MagicMock()
		doc.doctype = "Payment Entry"
		doc.name = "PE-x"
		sw, log, cached, enqueue = self._disabled()
		with (
			sw,
			log as mock_log,
			cached,
			enqueue as mock_enqueue,
			patch.object(
				payment_entry.payment_status_resolver, "resolve_status_for_wave_order"
			) as mock_resolve,
		):
			payment_entry.on_payment_entry_submit(doc)
		mock_enqueue.assert_not_called()
		mock_resolve.assert_not_called()
		self.assertTrue(self._logged_master_disabled(mock_log))

	def test_after_pick_list_insert_skips_all_channels(self):
		"""Pick List after_insert short-circuits status + batch-IDs + amend channels at once."""
		doc = MagicMock()
		doc.doctype = "Pick List"
		doc.name = "PL-x"
		sw, log, cached, enqueue = self._disabled()
		with (
			sw,
			log as mock_log,
			cached,
			enqueue as mock_enqueue,
			patch.object(pl_handler.order_status, "dispatch_with_wave_order_ids") as mock_dispatch,
		):
			pl_handler.after_pick_list_insert(doc)
		mock_enqueue.assert_not_called()
		mock_dispatch.assert_not_called()
		self.assertTrue(self._logged_master_disabled(mock_log))

	def test_shipday_order_stage_skips_wave_push_but_runs_upstream(self):
		"""Shipday override: upstream order_stage_tracker ALWAYS runs; only the Wave push is gated."""
		upstream_result = {"new_stage": "Delivered", "sales_order": "SO-x"}
		with (
			patch.object(
				shipday_intercept, "_upstream_order_stage", return_value=upstream_result
			) as mock_upstream,
			patch.object(frappe.db, "get_value", return_value="wave-x"),
			patch.object(master_switch, "is_wave_integration_enabled", return_value=False),
			patch.object(master_switch, "log_step") as mock_log,
			patch.object(shipday_intercept.order_status, "dispatch_with_wave_order_ids") as mock_dispatch,
			patch.object(frappe, "get_doc") as mock_get_doc,
		):
			result = shipday_intercept.order_stage("DN-x")
		mock_upstream.assert_called_once_with("DN-x")
		mock_dispatch.assert_not_called()
		mock_get_doc.assert_not_called()
		self.assertEqual(result, upstream_result)
		self.assertTrue(self._logged_master_disabled(mock_log))
