"""Unit tests for the Pick List outbound pipeline.

Two layers:

  1. The validate hook (`stamp_wave_order_id`) — pulls the linked SO's
     wave_order_id onto the Pick List, handles the multi-SO case.
  2. The after_insert hook (`after_pick_list_insert`) — dispatches the status
     channel via the rules table (no forced_payload) and gates the batch-IDs
     channel on the pick_list_batch_ids_push_enabled Check.

Whether ACCEPTED actually fires belongs to the rule resolver and is covered
by test_order_status_push.py; here we patch dispatch_with_wave_order_ids at
the module boundary so each test stays focused on the Pick List handler's
plumbing.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers import order_status
from wave_sync_hypa.wave_sync_hypa.handlers import pick_list as pl_handler

WAVE_ID_A = "wave-id-aaa"
WAVE_ID_B = "wave-id-bbb"


def _pl(
	locations: list[dict] | None = None,
	wave_order_id: str = "",
	name: str = "PICK-2026-0001",
) -> SimpleNamespace:
	"""Fabricate a Pick List stand-in with .doctype, .name, .get(), .wave_order_id."""
	doc = SimpleNamespace(doctype="Pick List", name=name, wave_order_id=wave_order_id)
	values = {"wave_order_id": wave_order_id, "locations": locations or []}

	def _get(key, default=None):
		return values.get(key, default)

	doc.get = _get
	return doc


def _row(sales_order: str | None, item_code: str = "", batch_no: str = "", qty: float = 0) -> dict:
	"""Pick List Item row carrying just enough surface for the handler to walk SO + item + batch."""
	return {
		"sales_order": sales_order or "",
		"item_code": item_code,
		"batch_no": batch_no,
		"qty": qty,
	}


def _settings(batch_ids_enabled: int = 0, picker_identifier_source: str = "") -> MagicMock:
	"""Wave Settings stand-in: master toggle + picker_identifier_source mode."""
	values = {
		"pick_list_batch_ids_push_enabled": batch_ids_enabled,
		"picker_identifier_source": picker_identifier_source,
	}
	settings = MagicMock(name="WaveSettings")
	settings.get.side_effect = lambda key, default=None: values.get(key, default)
	return settings


class TestStampWaveOrderId(FrappeTestCase):
	"""validate hook: idempotent, walks linked SOs, stamps doc.wave_order_id."""

	def test_no_op_when_field_already_populated(self):
		doc = _pl(locations=[_row("SO-001")], wave_order_id="prior-value")
		with (
			patch.object(frappe.db, "get_value") as mock_get_value,
			patch.object(pl_handler, "log_step") as mock_log,
		):
			pl_handler.stamp_wave_order_id(doc)

		self.assertEqual(doc.wave_order_id, "prior-value")
		mock_get_value.assert_not_called()
		mock_log.assert_not_called()

	def test_stamps_single_source_so_wave_order_id(self):
		doc = _pl(locations=[_row("SO-001"), _row("SO-001")])

		def _get(_doctype, key, field, *a, **kw):
			if field == "wave_order_id":
				return WAVE_ID_A
			if field == "wave_friendly_id":
				return "10000111"
			return None

		with (
			patch.object(frappe.db, "get_value", side_effect=_get),
			patch.object(pl_handler, "log_step") as mock_log,
		):
			pl_handler.stamp_wave_order_id(doc)

		self.assertEqual(doc.wave_order_id, WAVE_ID_A)
		self.assertEqual(doc.wave_friendly_id, "10000111")
		mock_log.assert_not_called()

	def test_stamps_first_when_multiple_distinct_wave_orders_and_warns(self):
		doc = _pl(locations=[_row("SO-001"), _row("SO-002")])

		def _by_so(*args, **kwargs):
			# Friendly-id lookup: filter dict {"wave_order_id": <id>} keyed by WAVE_ID_A.
			if isinstance(args[1], dict):
				return {WAVE_ID_A: "10000222"}.get(args[1].get("wave_order_id"))
			return {"SO-001": WAVE_ID_A, "SO-002": WAVE_ID_B}.get(args[1])

		with (
			patch.object(frappe.db, "get_value", side_effect=_by_so),
			patch.object(pl_handler, "log_step") as mock_log,
		):
			pl_handler.stamp_wave_order_id(doc)

		self.assertEqual(doc.wave_order_id, WAVE_ID_A)
		self.assertEqual(doc.wave_friendly_id, "10000222")
		warnings = [
			c for c in mock_log.call_args_list
			if c.kwargs.get("step") == pl_handler.STEP_STAMP_MULTI_SOURCE
		]
		self.assertEqual(len(warnings), 1)
		self.assertEqual(warnings[0].kwargs.get("level"), "Warning")
		self.assertEqual(
			warnings[0].kwargs["request_body"]["wave_order_ids"], [WAVE_ID_A, WAVE_ID_B]
		)

	def test_no_op_when_pick_has_no_wave_sourced_so(self):
		doc = _pl(locations=[_row("SO-NON-WAVE"), _row(None)])
		with (
			patch.object(frappe.db, "get_value", return_value=None),
			patch.object(pl_handler, "log_step") as mock_log,
		):
			pl_handler.stamp_wave_order_id(doc)

		self.assertEqual(doc.wave_order_id, "")
		mock_log.assert_not_called()


class TestAfterPickListInsert(FrappeTestCase):
	"""after_insert hook: rule-driven status dispatch + Check-gated batch-IDs stub."""

	def test_dispatches_status_for_every_wave_linked_pick_list(self):
		"""Status channel fires unconditionally — rule resolver decides whether ACCEPTED is sent."""
		doc = _pl(locations=[_row("SO-001"), _row("SO-002")])

		def _by_so(*args, **kwargs):
			return {"SO-001": WAVE_ID_A, "SO-002": WAVE_ID_B}.get(args[1])

		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(batch_ids_enabled=0)),
			patch.object(frappe.db, "get_value", side_effect=_by_so),
			patch.object(order_status, "dispatch_with_wave_order_ids") as mock_dispatch,
			patch.object(pl_handler, "log_step"),
		):
			pl_handler.after_pick_list_insert(doc)

		mock_dispatch.assert_called_once()
		args, kwargs = mock_dispatch.call_args
		self.assertEqual(args[1], "after_insert")
		self.assertEqual(args[2], [WAVE_ID_A, WAVE_ID_B])
		# No forced_payload — the rule resolver is authoritative.
		self.assertNotIn("forced_payload", kwargs)

	def test_skips_when_no_wave_linked_so(self):
		"""Pick List with no Wave linkage -> STEP_NO_WAVE_ORDERS, no dispatch, no batch-IDs."""
		doc = _pl(locations=[_row("SO-NON-WAVE")], wave_order_id="")
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(batch_ids_enabled=1)),
			patch.object(frappe.db, "get_value", return_value=None),
			patch.object(order_status, "dispatch_with_wave_order_ids") as mock_dispatch,
			patch.object(pl_handler, "log_step") as mock_log,
		):
			pl_handler.after_pick_list_insert(doc)

		mock_dispatch.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertEqual(steps, [pl_handler.STEP_NO_WAVE_ORDERS])

	def test_falls_back_to_stamped_wave_order_id_when_locations_empty(self):
		"""Stamped wave_order_id but rows lack sales_order -> dispatcher still gets the id."""
		doc = _pl(locations=[_row(None)], wave_order_id=WAVE_ID_A)
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(batch_ids_enabled=0)),
			patch.object(frappe.db, "get_value", return_value=None),
			patch.object(order_status, "dispatch_with_wave_order_ids") as mock_dispatch,
			patch.object(pl_handler, "log_step"),
		):
			pl_handler.after_pick_list_insert(doc)

		mock_dispatch.assert_called_once()
		self.assertEqual(mock_dispatch.call_args.args[2], [WAVE_ID_A])

	def test_batch_ids_check_on_enqueues_one_worker_per_wave_order_with_grouped_products(self):
		"""Check on: one frappe.enqueue per Wave order, each with grouped+deduped products_data."""
		# Two SOs map to two Wave orders. Two rows per SO with item batches.
		doc = _pl(locations=[
			_row("SO-001", item_code="JTD011", batch_no="B-001", qty=3),
			_row("SO-001", item_code="JTD011", batch_no="B-001", qty=3),  # dup -> deduped batch
			_row("SO-001", item_code="MILK", batch_no="B-M-9", qty=2),
			_row("SO-002", item_code="JTD011", batch_no="B-X", qty=4),
		])

		def _by_so(*args, **kwargs):
			return {"SO-001": WAVE_ID_A, "SO-002": WAVE_ID_B}.get(args[1])

		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(batch_ids_enabled=1)),
			patch.object(frappe.db, "get_value", side_effect=_by_so),
			patch.object(order_status, "dispatch_with_wave_order_ids"),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(pl_handler, "log_step"),
		):
			pl_handler.after_pick_list_insert(doc)

		# One enqueue per Wave order.
		self.assertEqual(mock_enqueue.call_count, 2)
		by_wave = {c.kwargs["wave_order_id"]: c.kwargs for c in mock_enqueue.call_args_list}
		# Wave-A: JTD011 picked from B-001 (deduped) + MILK picked from B-M-9. Comments list every row.
		self.assertEqual(
			by_wave[WAVE_ID_A]["products_data"],
			[
				{"item_code": "JTD011", "batch_ids": ["B-001"], "comments": "- B-001: 3\n- B-001: 3"},
				{"item_code": "MILK", "batch_ids": ["B-M-9"], "comments": "- B-M-9: 2"},
			],
		)
		# Wave-B: just JTD011 from B-X.
		self.assertEqual(
			by_wave[WAVE_ID_B]["products_data"],
			[{"item_code": "JTD011", "batch_ids": ["B-X"], "comments": "- B-X: 4"}],
		)
		# enqueue_after_commit so the worker only fires after the in-memory PL is persisted.
		for c in mock_enqueue.call_args_list:
			self.assertTrue(c.kwargs["enqueue_after_commit"])
			self.assertEqual(c.kwargs["pick_list_name"], doc.name)

	def test_batch_ids_check_off_does_not_enqueue(self):
		"""Check off: status dispatcher still called, batch worker never enqueued."""
		doc = _pl(locations=[_row("SO-001", item_code="JTD011", batch_no="B-001")])
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(batch_ids_enabled=0)),
			patch.object(frappe.db, "get_value", return_value=WAVE_ID_A),
			patch.object(order_status, "dispatch_with_wave_order_ids") as mock_dispatch,
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(pl_handler, "log_step"),
		):
			pl_handler.after_pick_list_insert(doc)

		mock_dispatch.assert_called_once()
		mock_enqueue.assert_not_called()

	def test_batch_ids_no_batches_logs_skip_no_enqueue(self):
		"""Pick List rows with no batch_no -> no_batches_to_push log, no enqueue."""
		doc = _pl(locations=[_row("SO-001", item_code="JTD011", batch_no="")])
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(batch_ids_enabled=1)),
			patch.object(frappe.db, "get_value", return_value=WAVE_ID_A),
			patch.object(order_status, "dispatch_with_wave_order_ids"),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(pl_handler, "log_step") as mock_log,
		):
			pl_handler.after_pick_list_insert(doc)

		mock_enqueue.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pl_handler.STEP_BATCH_IDS_NO_BATCHES_TO_PUSH, steps)

	def test_item_code_source_enqueues_sku_consolidated_payload(self):
		"""picker_identifier_source = 'Item Code' -> one entry per SKU with [item_code]; comments carry ERP batch truth."""
		doc = _pl(locations=[
			_row("SO-001", item_code="JTD011", batch_no="B-001", qty=3),
			_row("SO-001", item_code="JTD011", batch_no="B-002", qty=2),
		])
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(batch_ids_enabled=1, picker_identifier_source="Item Code")),
			patch.object(frappe.db, "get_value", return_value=WAVE_ID_A),
			patch.object(order_status, "dispatch_with_wave_order_ids"),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(pl_handler, "log_step"),
		):
			pl_handler.after_pick_list_insert(doc)
		mock_enqueue.assert_called_once()
		self.assertEqual(
			mock_enqueue.call_args.kwargs["products_data"],
			[{"item_code": "JTD011", "batch_ids": ["JTD011"], "comments": "- B-001: 3\n- B-002: 2"}],
		)

	def test_item_barcode_source_enqueues_first_barcode_per_sku(self):
		"""picker_identifier_source = 'Item Barcode' -> first Item Barcode row's value, one per SKU; comments still carry batches."""
		doc = _pl(locations=[
			_row("SO-001", item_code="JTD011", batch_no="B-001", qty=3),
			_row("SO-001", item_code="JTD011", batch_no="B-002", qty=2),
		])
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(batch_ids_enabled=1, picker_identifier_source="Item Barcode")),
			patch.object(frappe.db, "get_value", return_value=WAVE_ID_A),
			patch.object(frappe, "get_all", return_value=[{"barcode": "5901234123457"}]),
			patch.object(order_status, "dispatch_with_wave_order_ids"),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(pl_handler, "log_step"),
		):
			pl_handler.after_pick_list_insert(doc)
		mock_enqueue.assert_called_once()
		self.assertEqual(
			mock_enqueue.call_args.kwargs["products_data"],
			[{"item_code": "JTD011", "batch_ids": ["5901234123457"], "comments": "- B-001: 3\n- B-002: 2"}],
		)

	def test_item_barcode_source_missing_barcode_logs_error_and_skips(self):
		"""picker_identifier_source = 'Item Barcode' + Item has no barcode -> Error row, SKU dropped, others still push."""
		doc = _pl(locations=[
			_row("SO-001", item_code="NO-BARCODE", batch_no="B-001", qty=1),
			_row("SO-001", item_code="JTD011", batch_no="B-002", qty=4),
		])

		def _barcode_lookup(*args, **kwargs):
			filters = kwargs.get("filters") or {}
			parent = filters.get("parent")
			if parent == "JTD011":
				return [{"barcode": "5901234123457"}]
			return []

		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(batch_ids_enabled=1, picker_identifier_source="Item Barcode")),
			patch.object(frappe.db, "get_value", return_value=WAVE_ID_A),
			patch.object(frappe, "get_all", side_effect=_barcode_lookup),
			patch.object(order_status, "dispatch_with_wave_order_ids"),
			patch.object(frappe, "enqueue") as mock_enqueue,
			patch.object(pl_handler, "log_step") as mock_log,
		):
			pl_handler.after_pick_list_insert(doc)
		# The well-configured SKU still goes out; the broken one is dropped + logged.
		mock_enqueue.assert_called_once()
		self.assertEqual(
			mock_enqueue.call_args.kwargs["products_data"],
			[{"item_code": "JTD011", "batch_ids": ["5901234123457"], "comments": "- B-002: 4"}],
		)
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pl_handler.STEP_BATCH_IDS_IDENTIFIER_FAILED, steps)
