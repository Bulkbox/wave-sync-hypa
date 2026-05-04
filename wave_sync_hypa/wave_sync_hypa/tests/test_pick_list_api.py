"""Unit tests for api.pick_list.push_batch_ids_now (manual button endpoint).

Covers permission check, no-Wave-orders short-circuit, no-batches short-circuit,
happy-path enqueue with manual_trigger=True, and structured response shape.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.api import pick_list as pl_api
from wave_sync_hypa.wave_sync_hypa.handlers import pick_list as pl_handler

WAVE_ID_A = "wave-id-aaa"
WAVE_ID_B = "wave-id-bbb"


def _row(sales_order: str | None, item_code: str = "", batch_no: str = "") -> dict:
	return {
		"sales_order": sales_order or "",
		"item_code": item_code,
		"batch_no": batch_no,
	}


def _pl_doc(locations: list[dict] | None = None, wave_order_id: str = "") -> MagicMock:
	"""Frappe Pick List doc stand-in with .name/.doctype/.get()/.check_permission()."""
	doc = MagicMock(name="PickListDoc")
	doc.name = "PICK-2026-0001"
	doc.doctype = "Pick List"
	values = {"wave_order_id": wave_order_id, "locations": locations or []}
	doc.get.side_effect = lambda key, default=None: values.get(key, default)
	doc.check_permission.return_value = None
	return doc


class TestPushBatchIdsNow(FrappeTestCase):
	"""Manual-trigger endpoint: enqueue per Wave order, bypass kill-switch."""

	def test_returns_not_ok_when_pick_list_has_no_wave_orders(self):
		doc = _pl_doc(locations=[_row("SO-NON-WAVE", item_code="X", batch_no="B-1")])
		with (
			patch.object(frappe, "get_doc", return_value=doc),
			patch.object(frappe.db, "get_value", return_value=None),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(pl_api, "log_step") as mock_log,
		):
			result = pl_api.push_batch_ids_now("PICK-2026-0001")
		self.assertFalse(result["ok"])
		self.assertIn("Wave-sourced", result["reason"])
		mock_enqueue.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pl_api.STEP_MANUAL_TRIGGER_NO_WAVE_ORDERS, steps)

	def test_returns_not_ok_when_no_items_have_batches(self):
		"""Pick List has Wave-sourced rows but none carry batch_no -> nothing to PATCH."""
		doc = _pl_doc(locations=[_row("SO-001", item_code="JTD011", batch_no="")])
		with (
			patch.object(frappe, "get_doc", return_value=doc),
			patch.object(frappe.db, "get_value", return_value=WAVE_ID_A),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(pl_api, "log_step") as mock_log,
		):
			result = pl_api.push_batch_ids_now("PICK-2026-0001")
		self.assertFalse(result["ok"])
		mock_enqueue.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pl_handler.STEP_BATCH_IDS_NO_BATCHES_TO_PUSH, steps)

	def test_enqueues_one_worker_per_wave_order_with_manual_trigger_flag(self):
		"""Happy path: per-Wave-order enqueue with manual_trigger=True kwarg set."""
		doc = _pl_doc(locations=[
			_row("SO-001", item_code="JTD011", batch_no="B-001"),
			_row("SO-001", item_code="MILK", batch_no="B-M-9"),
			_row("SO-002", item_code="JTD011", batch_no="B-X"),
		])

		def _by_so(*args, **kwargs):
			return {"SO-001": WAVE_ID_A, "SO-002": WAVE_ID_B}.get(args[1])

		with (
			patch.object(frappe, "get_doc", return_value=doc),
			patch.object(frappe.db, "get_value", side_effect=_by_so),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(pl_api, "log_step"),
		):
			result = pl_api.push_batch_ids_now("PICK-2026-0001")

		self.assertTrue(result["ok"])
		self.assertEqual(result["enqueued"], 2)
		self.assertEqual(mock_enqueue.call_count, 2)
		for call in mock_enqueue.call_args_list:
			self.assertTrue(call.kwargs["manual_trigger"])
			self.assertEqual(call.kwargs["pick_list_name"], "PICK-2026-0001")
		# Each call carries one Wave order id.
		seen = {c.kwargs["wave_order_id"] for c in mock_enqueue.call_args_list}
		self.assertEqual(seen, {WAVE_ID_A, WAVE_ID_B})

	def test_check_permission_called_with_write(self):
		"""Endpoint enforces write permission on the Pick List."""
		doc = _pl_doc(locations=[_row("SO-001", item_code="JTD011", batch_no="B-001")])
		with (
			patch.object(frappe, "get_doc", return_value=doc),
			patch.object(frappe.db, "get_value", return_value=WAVE_ID_A),
			patch.object(frappe, "enqueue"),
			patch.object(pl_api, "log_step"),
		):
			pl_api.push_batch_ids_now("PICK-2026-0001")
		doc.check_permission.assert_called_once_with("write")
