"""Unit tests for the Sales Invoice status-push pipeline.

Three concerns covered:

  1. stamp_wave_order_id walks SI items via sales_order first, then
     delivery_note as fallback.
  2. on_sales_invoice_submit fans out UNDER_DELIVERY for regular invoices.
  3. on_sales_invoice_submit routes return invoices through the credit-note
     classifier — full-value -> CANCELLED via forced payload, partial /
     unclassifiable -> STEP_SKIPPED_PARTIAL_RETURN.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers import order_status
from wave_sync_hypa.wave_sync_hypa.handlers import sales_invoice as si_handler

WAVE_ID_A = "wave-id-aaa"
WAVE_ID_B = "wave-id-bbb"


def _si(
	items,
	wave_order_id="",
	is_return=0,
	return_against="",
	grand_total=0.0,
	name="SI-2026-0001",
) -> SimpleNamespace:
	doc = SimpleNamespace(doctype="Sales Invoice", name=name, wave_order_id=wave_order_id)
	values = {
		"wave_order_id": wave_order_id,
		"items": items,
		"is_return": is_return,
		"return_against": return_against,
		"grand_total": grand_total,
	}

	def _get(key, default=None):
		return values.get(key, default)

	doc.get = _get
	return doc


def _item(*, sales_order=None, delivery_note=None) -> dict:
	return {"sales_order": sales_order or "", "delivery_note": delivery_note or ""}


class TestStampWaveOrderId(FrappeTestCase):
	"""validate hook: idempotent, walks SO first then DN, dedupes, stamps first match."""

	def test_no_op_when_field_already_populated(self):
		doc = _si(items=[_item(sales_order="SO-001")], wave_order_id="prior")
		with (
			patch.object(frappe.db, "get_value") as mock_get_value,
			patch.object(si_handler, "log_step") as mock_log,
		):
			si_handler.stamp_wave_order_id(doc)

		self.assertEqual(doc.wave_order_id, "prior")
		mock_get_value.assert_not_called()
		mock_log.assert_not_called()

	def test_resolves_via_sales_order_link(self):
		"""SI made from SO directly: items[].sales_order -> SO.wave_order_id."""
		doc = _si(items=[_item(sales_order="SO-001")])
		with (
			patch.object(frappe.db, "get_value", return_value=WAVE_ID_A) as mock_get,
			patch.object(si_handler, "log_step") as mock_log,
		):
			si_handler.stamp_wave_order_id(doc)

		self.assertEqual(doc.wave_order_id, WAVE_ID_A)
		mock_get.assert_called_once_with("Sales Order", "SO-001", "wave_order_id")
		mock_log.assert_not_called()

	def test_falls_back_to_delivery_note_link(self):
		"""SI made from DN: items[].sales_order empty, items[].delivery_note populated."""
		doc = _si(items=[_item(delivery_note="DN-001")])

		def _by_doctype(*args, **kwargs):
			# args = (doctype, name, fieldname)
			doctype = args[0]
			return {"Delivery Note": WAVE_ID_A}.get(doctype)

		with (
			patch.object(frappe.db, "get_value", side_effect=_by_doctype),
			patch.object(si_handler, "log_step"),
		):
			si_handler.stamp_wave_order_id(doc)

		self.assertEqual(doc.wave_order_id, WAVE_ID_A)

	def test_multi_source_warns_and_stamps_first(self):
		"""SI bridging two Wave SOs -> first stamped, Warning row enumerates both."""
		doc = _si(items=[_item(sales_order="SO-001"), _item(sales_order="SO-002")])

		def _by_so(*args, **kwargs):
			so_name = args[1]
			return {"SO-001": WAVE_ID_A, "SO-002": WAVE_ID_B}.get(so_name)

		with (
			patch.object(frappe.db, "get_value", side_effect=_by_so),
			patch.object(si_handler, "log_step") as mock_log,
		):
			si_handler.stamp_wave_order_id(doc)

		self.assertEqual(doc.wave_order_id, WAVE_ID_A)
		warns = [
			c for c in mock_log.call_args_list
			if c.kwargs.get("step") == si_handler.STEP_STAMP_MULTI_SOURCE
		]
		self.assertEqual(len(warns), 1)
		self.assertEqual(warns[0].kwargs["request_body"]["wave_order_ids"], [WAVE_ID_A, WAVE_ID_B])


class TestOnSalesInvoiceSubmit(FrappeTestCase):
	"""submit hook: dispatch fan-out for regular invoices, explicit skip for returns."""

	def test_dispatches_with_distinct_wave_order_ids(self):
		"""Two items reaching two distinct Wave SOs -> dispatcher receives both ids."""
		doc = _si(items=[_item(sales_order="SO-001"), _item(sales_order="SO-002")])

		def _by_so(*args, **kwargs):
			so_name = args[1]
			return {"SO-001": WAVE_ID_A, "SO-002": WAVE_ID_B}.get(so_name)

		with (
			patch.object(frappe.db, "get_value", side_effect=_by_so),
			patch.object(order_status, "dispatch_with_wave_order_ids") as mock_dispatch,
		):
			si_handler.on_sales_invoice_submit(doc)

		mock_dispatch.assert_called_once()
		args, _ = mock_dispatch.call_args
		self.assertEqual(args[1], "submit")
		self.assertEqual(args[2], [WAVE_ID_A, WAVE_ID_B])

	def test_falls_back_to_stamped_field_when_items_lack_links(self):
		doc = _si(items=[_item()], wave_order_id=WAVE_ID_A)
		with (
			patch.object(frappe.db, "get_value", return_value=None),
			patch.object(order_status, "dispatch_with_wave_order_ids") as mock_dispatch,
		):
			si_handler.on_sales_invoice_submit(doc)

		mock_dispatch.assert_called_once()
		self.assertEqual(mock_dispatch.call_args.args[2], [WAVE_ID_A])

	def test_full_value_credit_note_dispatches_cancelled_with_credit_note_event(self):
		"""is_return=1 + classifier says full-value -> dispatch CANCELLED with credit_note_submit event."""
		doc = _si(
			items=[_item(sales_order="SO-001")],
			wave_order_id=WAVE_ID_A,
			is_return=1,
			return_against="SI-ORIG",
			grand_total=-1234.56,
		)
		with (
			patch.object(frappe.db, "get_value", return_value=WAVE_ID_A),
			patch.object(
				si_handler.credit_note_classifier,
				"is_full_value_credit_note",
				return_value=True,
			) as mock_classifier,
			patch.object(order_status, "dispatch_with_wave_order_ids") as mock_dispatch,
			patch.object(si_handler, "log_step") as mock_log,
		):
			si_handler.on_sales_invoice_submit(doc)

		mock_classifier.assert_called_once_with(doc)
		mock_dispatch.assert_called_once()
		args, kwargs = mock_dispatch.call_args
		self.assertEqual(args[1], si_handler.EVENT_CREDIT_NOTE_SUBMIT)
		self.assertEqual(args[2], [WAVE_ID_A])
		self.assertEqual(kwargs.get("forced_payload"), {"status": "CANCELLED"})
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(si_handler.STEP_FULL_RETURN_DETECTED, steps)

	def test_partial_credit_note_skips_with_partial_return_step(self):
		"""is_return=1 + classifier says partial -> log STEP_SKIPPED_PARTIAL_RETURN, no dispatch."""
		doc = _si(
			items=[_item(sales_order="SO-001")],
			wave_order_id=WAVE_ID_A,
			is_return=1,
			return_against="SI-ORIG",
			grand_total=-100.00,  # partial value
		)
		with (
			patch.object(frappe.db, "get_value", return_value=WAVE_ID_A),
			patch.object(
				si_handler.credit_note_classifier,
				"is_full_value_credit_note",
				return_value=False,
			),
			patch.object(order_status, "dispatch_with_wave_order_ids") as mock_dispatch,
			patch.object(si_handler, "log_step") as mock_log,
		):
			si_handler.on_sales_invoice_submit(doc)

		mock_dispatch.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(si_handler.STEP_SKIPPED_PARTIAL_RETURN, steps)

	def test_return_invoice_with_no_return_against_skips_as_partial(self):
		"""Malformed return (is_return=1 but no return_against): classifier returns False -> partial path."""
		doc = _si(
			items=[_item(sales_order="SO-001")],
			wave_order_id=WAVE_ID_A,
			is_return=1,
			return_against="",
			grand_total=-50.0,
		)
		with (
			patch.object(frappe.db, "get_value", return_value=WAVE_ID_A),
			patch.object(
				si_handler.credit_note_classifier,
				"is_full_value_credit_note",
				return_value=False,
			),
			patch.object(order_status, "dispatch_with_wave_order_ids") as mock_dispatch,
			patch.object(si_handler, "log_step") as mock_log,
		):
			si_handler.on_sales_invoice_submit(doc)

		mock_dispatch.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(si_handler.STEP_SKIPPED_PARTIAL_RETURN, steps)

	def test_dispatches_empty_list_when_si_is_not_wave_linked(self):
		"""Non-Wave SI: dispatcher invoked with [], will log SKIPPED_NO_WAVE_ID itself."""
		doc = _si(items=[_item(sales_order="SO-NON-WAVE")])
		with (
			patch.object(frappe.db, "get_value", return_value=None),
			patch.object(order_status, "dispatch_with_wave_order_ids") as mock_dispatch,
		):
			si_handler.on_sales_invoice_submit(doc)

		mock_dispatch.assert_called_once()
		self.assertEqual(mock_dispatch.call_args.args[2], [])
