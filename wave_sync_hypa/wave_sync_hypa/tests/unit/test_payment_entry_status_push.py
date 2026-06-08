"""Unit tests for the Payment Entry handler.

Two layers:

  1. The validate hook (`stamp_wave_order_id`) — walks `references[]` and
     stamps `wave_order_id` from the first reachable SI/SO.
  2. The on_submit hook (`on_payment_entry_submit`) — per Wave order asks
     payment_status_resolver, and if it returns "COMPLETED" enqueues the
     payment_status_pusher to PATCH Wave's paymentStatus. None means
     partial/zero/unresolvable -> logged skip, no enqueue.

The resolver and pusher are patched at the module boundary so each test
focuses on the handler's branching.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers import payment_entry as pe_handler
from wave_sync_hypa.wave_sync_hypa.services import payment_status_pusher, payment_status_resolver

WAVE_ID_A = "wave-id-aaa"
WAVE_ID_B = "wave-id-bbb"


def _ref(reference_doctype: str, reference_name: str) -> dict:
	return {"reference_doctype": reference_doctype, "reference_name": reference_name}


def _pe(
	references: list[dict] | None = None,
	wave_order_id: str = "",
	payment_type: str = "Receive",
	name: str = "PE-2026-0001",
) -> SimpleNamespace:
	doc = SimpleNamespace(doctype="Payment Entry", name=name, wave_order_id=wave_order_id)
	values = {
		"references": references or [],
		"wave_order_id": wave_order_id,
		"payment_type": payment_type,
	}
	doc.get = lambda key, default=None: values.get(key, default)
	return doc


class TestStampWaveOrderId(FrappeTestCase):
	"""validate hook: idempotent, walks references, stamps doc.wave_order_id."""

	def test_no_op_when_field_already_populated(self):
		doc = _pe(references=[_ref("Sales Invoice", "SI-001")], wave_order_id="prior")
		with (
			patch.object(frappe.db, "get_value") as mock_get_value,
			patch.object(pe_handler, "log_step") as mock_log,
		):
			pe_handler.stamp_wave_order_id(doc)
		self.assertEqual(doc.wave_order_id, "prior")
		mock_get_value.assert_not_called()
		mock_log.assert_not_called()

	def test_stamps_first_reachable_wave_order_id(self):
		doc = _pe(references=[_ref("Sales Invoice", "SI-001"), _ref("Sales Invoice", "SI-002")])
		with (
			patch.object(frappe.db, "get_value", return_value=WAVE_ID_A),
			patch.object(pe_handler, "log_step") as mock_log,
		):
			pe_handler.stamp_wave_order_id(doc)
		self.assertEqual(doc.wave_order_id, WAVE_ID_A)
		mock_log.assert_not_called()

	def test_warns_on_multi_source(self):
		doc = _pe(references=[_ref("Sales Invoice", "SI-001"), _ref("Sales Order", "SO-002")])

		def _gv(*args, **kwargs):
			# Friendly-id lookup uses a dict filter; skip that here.
			if args[2] != "wave_order_id" or not isinstance(args[1], str):
				return None
			return {("Sales Invoice", "SI-001"): WAVE_ID_A, ("Sales Order", "SO-002"): WAVE_ID_B}.get(
				(args[0], args[1])
			)

		with (
			patch.object(frappe.db, "get_value", side_effect=_gv),
			patch.object(pe_handler, "log_step") as mock_log,
		):
			pe_handler.stamp_wave_order_id(doc)

		self.assertEqual(doc.wave_order_id, WAVE_ID_A)
		warnings = [
			c for c in mock_log.call_args_list if c.kwargs.get("step") == pe_handler.STEP_STAMP_MULTI_SOURCE
		]
		self.assertEqual(len(warnings), 1)
		self.assertEqual(warnings[0].kwargs["request_body"]["wave_order_ids"], [WAVE_ID_A, WAVE_ID_B])

	def test_stamps_wave_friendly_id_from_source_so(self):
		"""After stamping wave_order_id, the handler also stamps wave_friendly_id from the SO."""
		doc = _pe(references=[_ref("Sales Invoice", "SI-001")])

		def _gv(*args, **kwargs):
			# pe_references walks Sales Invoice -> wave_order_id; we also need the
			# subsequent wave_friendly_id lookup against Sales Order.
			if args[2] == "wave_order_id":
				return WAVE_ID_A
			if args[2] == "wave_friendly_id":
				return (
					"10000099"
					if isinstance(args[1], dict) and args[1].get("wave_order_id") == WAVE_ID_A
					else None
				)
			return None

		with (
			patch.object(frappe.db, "get_value", side_effect=_gv),
			patch.object(pe_handler, "log_step"),
		):
			pe_handler.stamp_wave_order_id(doc)

		self.assertEqual(doc.wave_order_id, WAVE_ID_A)
		self.assertEqual(doc.wave_friendly_id, "10000099")

	def test_friendly_id_defaults_to_empty_when_so_lacks_one(self):
		"""SO exists but has no wave_friendly_id stamped -> friendly_id on the doc is ''."""
		doc = _pe(references=[_ref("Sales Invoice", "SI-001")])

		def _gv(*args, **kwargs):
			return WAVE_ID_A if args[2] == "wave_order_id" else None

		with (
			patch.object(frappe.db, "get_value", side_effect=_gv),
			patch.object(pe_handler, "log_step"),
		):
			pe_handler.stamp_wave_order_id(doc)

		self.assertEqual(doc.wave_order_id, WAVE_ID_A)
		self.assertEqual(doc.wave_friendly_id, "")

	def test_no_op_for_journal_entry_only_refs(self):
		"""Refs to non-Wave doctypes (Journal Entry, etc.) leave the field untouched."""
		doc = _pe(references=[_ref("Journal Entry", "JE-001")])
		with (
			patch.object(frappe.db, "get_value", return_value=None) as mock_get_value,
			patch.object(pe_handler, "log_step") as mock_log,
		):
			pe_handler.stamp_wave_order_id(doc)
		self.assertEqual(doc.wave_order_id, "")
		mock_get_value.assert_not_called()
		mock_log.assert_not_called()


class TestOnPaymentEntrySubmit(FrappeTestCase):
	"""on_submit: enqueue paymentStatus push only when resolver returns COMPLETED."""

	def setUp(self):
		# Hold the master kill switch open so the handler's own branching is what's
		# under test here; the disabled-path short-circuit is covered in
		# test_master_switch.TestDecisionLayerSkipsEnqueueWhenMasterOff.
		guard = patch.object(pe_handler, "skip_if_disabled", return_value=False)
		guard.start()
		self.addCleanup(guard.stop)

	def test_skipped_for_payment_type_pay(self):
		"""Refunds (payment_type=Pay) are silently skipped at the handler."""
		doc = _pe(references=[_ref("Sales Invoice", "SI-001")], payment_type="Pay")
		with (
			patch.object(frappe.db, "get_value") as mock_get_value,
			patch.object(payment_status_pusher, "enqueue_payment_status_push") as mock_enqueue,
			patch.object(pe_handler, "log_step") as mock_log,
		):
			pe_handler.on_payment_entry_submit(doc)
		mock_enqueue.assert_not_called()
		mock_get_value.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertEqual(steps, [pe_handler.STEP_SKIPPED_PAYMENT_TYPE])

	def test_enqueues_when_resolver_returns_completed(self):
		doc = _pe(references=[_ref("Sales Invoice", "SI-001")])
		with (
			patch.object(pe_handler, "_complete_on_payment_entry", return_value=False),
			patch.object(frappe.db, "get_value", return_value=WAVE_ID_A),
			patch.object(
				payment_status_resolver,
				"resolve_status_for_wave_order",
				return_value=payment_status_resolver.STATUS_COMPLETED,
			) as mock_resolver,
			patch.object(payment_status_pusher, "enqueue_payment_status_push") as mock_enqueue,
		):
			pe_handler.on_payment_entry_submit(doc)
		mock_enqueue.assert_called_once()
		args, kwargs = mock_enqueue.call_args
		self.assertEqual(args[1], WAVE_ID_A)
		self.assertEqual(args[2], "COMPLETED")
		self.assertIn("correlation_id", kwargs)
		mock_resolver.assert_called_once()

	def test_skip_with_audit_when_resolver_returns_none(self):
		"""Partial / zero / unresolvable -> log skip row, no enqueue."""
		doc = _pe(references=[_ref("Sales Invoice", "SI-001")])
		with (
			patch.object(frappe.db, "get_value", return_value=WAVE_ID_A),
			patch.object(
				payment_status_resolver,
				"resolve_status_for_wave_order",
				return_value=None,
			),
			patch.object(payment_status_pusher, "enqueue_payment_status_push") as mock_enqueue,
			patch.object(pe_handler, "log_step") as mock_log,
		):
			pe_handler.on_payment_entry_submit(doc)
		mock_enqueue.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pe_handler.STEP_SKIPPED_PARTIAL_OR_ZERO, steps)

	def test_per_wave_order_only_full_settlements_enqueue(self):
		"""One PE settling two Wave orders, one full + one partial -> only the full enqueues."""
		doc = _pe(references=[_ref("Sales Invoice", "SI-A"), _ref("Sales Invoice", "SI-B")])

		def _gv(*args, **kwargs):
			return {("Sales Invoice", "SI-A"): WAVE_ID_A, ("Sales Invoice", "SI-B"): WAVE_ID_B}.get(
				(args[0], args[1])
			)

		def _resolve(_pe_doc, wave_order_id):
			return payment_status_resolver.STATUS_COMPLETED if wave_order_id == WAVE_ID_A else None

		with (
			patch.object(pe_handler, "_complete_on_payment_entry", return_value=False),
			patch.object(frappe.db, "get_value", side_effect=_gv),
			patch.object(payment_status_resolver, "resolve_status_for_wave_order", side_effect=_resolve),
			patch.object(payment_status_pusher, "enqueue_payment_status_push") as mock_enqueue,
			patch.object(pe_handler, "log_step") as mock_log,
		):
			pe_handler.on_payment_entry_submit(doc)

		# WAVE_ID_A (full) enqueues; WAVE_ID_B (None) logs skip.
		mock_enqueue.assert_called_once()
		self.assertEqual(mock_enqueue.call_args.args[1], WAVE_ID_A)
		self.assertEqual(mock_enqueue.call_args.args[2], "COMPLETED")
		skip_rows = [
			c
			for c in mock_log.call_args_list
			if c.kwargs.get("step") == pe_handler.STEP_SKIPPED_PARTIAL_OR_ZERO
		]
		self.assertEqual(len(skip_rows), 1)
		self.assertEqual(skip_rows[0].kwargs.get("wave_id"), WAVE_ID_B)

	def test_no_op_when_no_wave_orders_reachable(self):
		"""PE with non-Wave refs: no enqueue, no resolver call."""
		doc = _pe(references=[_ref("Journal Entry", "JE-001")])
		with (
			patch.object(frappe.db, "get_value", return_value=None),
			patch.object(payment_status_resolver, "resolve_status_for_wave_order") as mock_resolver,
			patch.object(payment_status_pusher, "enqueue_payment_status_push") as mock_enqueue,
		):
			pe_handler.on_payment_entry_submit(doc)
		mock_enqueue.assert_not_called()
		mock_resolver.assert_not_called()

	def test_falls_back_to_stamped_wave_order_id_when_refs_have_none(self):
		"""References without wave_order_ids but doc has one stamped -> resolver consulted on that id."""
		doc = _pe(references=[_ref("Sales Invoice", "SI-001")], wave_order_id=WAVE_ID_A)
		with (
			patch.object(pe_handler, "_complete_on_payment_entry", return_value=False),
			patch.object(frappe.db, "get_value", return_value=None),
			patch.object(
				payment_status_resolver,
				"resolve_status_for_wave_order",
				return_value=payment_status_resolver.STATUS_COMPLETED,
			),
			patch.object(payment_status_pusher, "enqueue_payment_status_push") as mock_enqueue,
		):
			pe_handler.on_payment_entry_submit(doc)
		mock_enqueue.assert_called_once()
		self.assertEqual(mock_enqueue.call_args.args[1], WAVE_ID_A)
