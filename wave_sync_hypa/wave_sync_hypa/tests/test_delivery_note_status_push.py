"""Unit tests for the Delivery Note status-push pipeline.

Covers two layers:

  1. The validate hook (`stamp_wave_order_id`) — pulls the source SO's
     wave_order_id onto the DN, handles the multi-SO case with a warning row.
  2. The submit hook (`on_delivery_note_submit`) — fans out one push per
     distinct wave_order_id reachable from items[].against_sales_order.

The dispatcher itself is exercised by test_order_status_push.py; here we
patch it at the module boundary so each test stays focused on the DN
plumbing.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers import delivery_note as dn_handler
from wave_sync_hypa.wave_sync_hypa.handlers import order_status

WAVE_ID_A = "wave-id-aaa"
WAVE_ID_B = "wave-id-bbb"


def _dn(items: list[dict] | None = None, wave_order_id: str = "", name: str = "DN-2026-0001") -> SimpleNamespace:
	"""Fabricate a Delivery Note stand-in with .doctype, .name, .get(), and .wave_order_id."""
	doc = SimpleNamespace(doctype="Delivery Note", name=name, wave_order_id=wave_order_id)
	values = {"wave_order_id": wave_order_id, "items": items or []}

	def _get(key, default=None):
		return values.get(key, default)

	doc.get = _get
	return doc


def _item(against_sales_order: str | None) -> dict:
	"""DN item row carrying just enough surface for the handler to walk to the SO."""
	return {"against_sales_order": against_sales_order or ""}


class TestStampWaveOrderId(FrappeTestCase):
	"""validate hook: idempotent, walks source SOs, stamps doc.wave_order_id."""

	def test_no_op_when_field_already_populated(self):
		"""If wave_order_id is already on the DN, the hook does nothing."""
		doc = _dn(items=[_item("SO-001")], wave_order_id="prior-value")
		with (
			patch.object(frappe.db, "get_value") as mock_get_value,
			patch.object(dn_handler, "log_step") as mock_log,
		):
			dn_handler.stamp_wave_order_id(doc)

		self.assertEqual(doc.wave_order_id, "prior-value")
		mock_get_value.assert_not_called()
		mock_log.assert_not_called()

	def test_stamps_single_source_so_wave_order_id(self):
		"""Standard happy path: one SO -> wave_order_id resolved and assigned, no warning row."""
		doc = _dn(items=[_item("SO-001"), _item("SO-001")])  # same SO referenced twice
		with (
			patch.object(frappe.db, "get_value", return_value=WAVE_ID_A),
			patch.object(dn_handler, "log_step") as mock_log,
		):
			dn_handler.stamp_wave_order_id(doc)

		self.assertEqual(doc.wave_order_id, WAVE_ID_A)
		mock_log.assert_not_called()

	def test_stamps_first_when_multiple_distinct_wave_orders_and_warns(self):
		"""Multi-SO DN: first wave id stamped on the field, Warning row enumerates all distinct ids."""
		doc = _dn(items=[_item("SO-001"), _item("SO-002")])

		def _by_so(*args, **kwargs):
			# args are ("Sales Order", so_name, "wave_order_id")
			so_name = args[1]
			return {"SO-001": WAVE_ID_A, "SO-002": WAVE_ID_B}.get(so_name)

		with (
			patch.object(frappe.db, "get_value", side_effect=_by_so),
			patch.object(dn_handler, "log_step") as mock_log,
		):
			dn_handler.stamp_wave_order_id(doc)

		self.assertEqual(doc.wave_order_id, WAVE_ID_A)
		warnings = [
			c for c in mock_log.call_args_list
			if c.kwargs.get("step") == dn_handler.STEP_STAMP_MULTI_SOURCE
		]
		self.assertEqual(len(warnings), 1)
		self.assertEqual(warnings[0].kwargs.get("level"), "Warning")
		self.assertEqual(warnings[0].kwargs["request_body"]["wave_order_ids"], [WAVE_ID_A, WAVE_ID_B])

	def test_no_op_when_dn_has_no_wave_sourced_so(self):
		"""DN drawn from a non-Wave SO (or no SO link at all) leaves the field untouched."""
		doc = _dn(items=[_item("SO-NON-WAVE"), _item(None)])
		with (
			patch.object(frappe.db, "get_value", return_value=None),
			patch.object(dn_handler, "log_step") as mock_log,
		):
			dn_handler.stamp_wave_order_id(doc)

		self.assertEqual(doc.wave_order_id, "")
		mock_log.assert_not_called()


class TestOnDeliveryNoteSubmit(FrappeTestCase):
	"""submit hook: hands off to dispatch_with_wave_order_ids with the right id list."""

	def test_dispatches_with_distinct_wave_order_ids_from_items(self):
		"""Two items reaching two distinct Wave SOs -> dispatcher receives both ids in order."""
		doc = _dn(items=[_item("SO-001"), _item("SO-002")], wave_order_id=WAVE_ID_A)

		def _by_so(*args, **kwargs):
			so_name = args[1]
			return {"SO-001": WAVE_ID_A, "SO-002": WAVE_ID_B}.get(so_name)

		with (
			patch.object(frappe.db, "get_value", side_effect=_by_so),
			patch.object(order_status, "dispatch_with_wave_order_ids") as mock_dispatch,
		):
			dn_handler.on_delivery_note_submit(doc)

		mock_dispatch.assert_called_once()
		args, kwargs = mock_dispatch.call_args
		# args = (doc, event, wave_order_ids)
		self.assertEqual(args[1], "submit")
		self.assertEqual(args[2], [WAVE_ID_A, WAVE_ID_B])

	def test_falls_back_to_stamped_field_when_items_lack_so_link(self):
		"""DN with wave_order_id stamped but items[].against_sales_order empty -> dispatcher still gets the id."""
		doc = _dn(items=[_item(None)], wave_order_id=WAVE_ID_A)

		with (
			patch.object(frappe.db, "get_value", return_value=None),
			patch.object(order_status, "dispatch_with_wave_order_ids") as mock_dispatch,
		):
			dn_handler.on_delivery_note_submit(doc)

		mock_dispatch.assert_called_once()
		self.assertEqual(mock_dispatch.call_args.args[2], [WAVE_ID_A])

	def test_dispatches_empty_list_when_dn_is_not_wave_linked(self):
		"""DN with no Wave linkage anywhere -> dispatcher invoked with [], will log skip."""
		doc = _dn(items=[_item("SO-NON-WAVE")], wave_order_id="")

		with (
			patch.object(frappe.db, "get_value", return_value=None),
			patch.object(order_status, "dispatch_with_wave_order_ids") as mock_dispatch,
		):
			dn_handler.on_delivery_note_submit(doc)

		mock_dispatch.assert_called_once()
		self.assertEqual(mock_dispatch.call_args.args[2], [])


class TestDispatchFanOut(FrappeTestCase):
	"""dispatch_with_wave_order_ids: enqueues one push per id, single correlation_id throughout."""

	def test_two_ids_produce_two_enqueue_calls_with_same_correlation(self):
		"""Multi-leg fan-out: two wave_order_ids -> two _enqueue_push calls, shared correlation_id."""
		doc = _dn(items=[_item("SO-001"), _item("SO-002")])
		settings = MagicMock()
		settings.get.side_effect = lambda key, default=None: {
			"outbound_order_status_sync_enabled": 1,
		}.get(key, default)
		with (
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(
				order_status.order_status_resolver,
				"resolve_outbound_payload",
				return_value={"status": "INVOICING"},
			),
			patch.object(order_status, "_enqueue_push") as mock_enqueue,
			patch.object(order_status, "log_step"),
		):
			order_status.dispatch_with_wave_order_ids(
				doc, "submit", [WAVE_ID_A, WAVE_ID_B]
			)

		self.assertEqual(mock_enqueue.call_count, 2)
		# Each leg targets a distinct wave_order_id.
		wave_ids_seen = [c.args[4] for c in mock_enqueue.call_args_list]
		self.assertEqual(wave_ids_seen, [WAVE_ID_A, WAVE_ID_B])
		# Single correlation_id for the whole emit (chains setup + per-leg rows).
		correlations = {c.args[3] for c in mock_enqueue.call_args_list}
		self.assertEqual(len(correlations), 1)

	def test_empty_id_list_logs_no_wave_id_skip_and_no_enqueue(self):
		"""dispatch with [] -> STEP_SKIPPED_NO_WAVE_ID, dispatcher never calls _enqueue_push."""
		doc = _dn(items=[])
		settings = MagicMock()
		settings.get.side_effect = lambda key, default=None: {
			"outbound_order_status_sync_enabled": 1,
		}.get(key, default)
		with (
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(order_status, "_enqueue_push") as mock_enqueue,
			patch.object(order_status, "log_step") as mock_log,
		):
			order_status.dispatch_with_wave_order_ids(doc, "submit", [])

		mock_enqueue.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(order_status.STEP_SKIPPED_NO_WAVE_ID, steps)
