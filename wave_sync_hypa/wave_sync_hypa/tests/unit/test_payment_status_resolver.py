"""Unit tests for payment_status_resolver.resolve_status_for_wave_order.

Covers SI-only, SO-only, and mixed reference cases plus the no-match branch.
All `frappe.db.get_value` calls are patched so each test stays focused on
the resolver's classification logic.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.services import payment_status_resolver

WAVE_ID_A = "wave-id-aaa"
WAVE_ID_B = "wave-id-bbb"


def _ref(reference_doctype: str, reference_name: str) -> dict:
	"""Payment Entry reference row stand-in."""
	return {"reference_doctype": reference_doctype, "reference_name": reference_name}


def _pe(references: list[dict] | None = None) -> SimpleNamespace:
	"""Fabricate a Payment Entry stand-in with .doctype, .name, .get()."""
	doc = SimpleNamespace(doctype="Payment Entry", name="PE-2026-0001")
	values = {"references": references or []}
	doc.get = lambda key, default=None: values.get(key, default)
	return doc


def _gv_factory(
	si_outstanding: dict[str, float] | None = None,
	so_totals: dict[str, dict[str, float]] | None = None,
	wave_order_ids: dict[tuple[str, str], str] | None = None,
):
	"""Build a frappe.db.get_value side_effect replicating the three lookups the resolver does."""
	si_outstanding = si_outstanding or {}
	so_totals = so_totals or {}
	wave_order_ids = wave_order_ids or {}

	def _gv(*args, **kwargs):
		# Resolver uses two shapes:
		#   frappe.db.get_value(doctype, name, "wave_order_id")
		#   frappe.db.get_value("Sales Invoice", name, "outstanding_amount")
		#   frappe.db.get_value("Sales Order", name, ["grand_total", "advance_paid"], as_dict=True)
		doctype, name, fieldspec = args[0], args[1], args[2]
		if fieldspec == "wave_order_id":
			return wave_order_ids.get((doctype, name))
		if doctype == "Sales Invoice" and fieldspec == "outstanding_amount":
			return si_outstanding.get(name)
		if doctype == "Sales Order" and isinstance(fieldspec, list):
			row = so_totals.get(name)
			return row if row is not None else None
		return None

	return _gv


class TestResolveStatusForWaveOrder(FrappeTestCase):
	"""Pure resolver: classify based on outstanding_amount / advance_paid."""

	def test_returns_completed_when_only_si_refs_and_outstanding_zero(self):
		doc = _pe([_ref("Sales Invoice", "SI-001")])
		gv = _gv_factory(
			wave_order_ids={("Sales Invoice", "SI-001"): WAVE_ID_A},
			si_outstanding={"SI-001": 0.0},
		)
		with patch.object(frappe.db, "get_value", side_effect=gv):
			result = payment_status_resolver.resolve_status_for_wave_order(doc, WAVE_ID_A)
		self.assertEqual(result, payment_status_resolver.STATUS_COMPLETED)

	def test_returns_payment_pending_when_any_linked_si_still_outstanding(self):
		doc = _pe([_ref("Sales Invoice", "SI-001"), _ref("Sales Invoice", "SI-002")])
		gv = _gv_factory(
			wave_order_ids={
				("Sales Invoice", "SI-001"): WAVE_ID_A,
				("Sales Invoice", "SI-002"): WAVE_ID_A,
			},
			si_outstanding={"SI-001": 0.0, "SI-002": 12.34},
		)
		with patch.object(frappe.db, "get_value", side_effect=gv):
			result = payment_status_resolver.resolve_status_for_wave_order(doc, WAVE_ID_A)
		self.assertEqual(result, payment_status_resolver.STATUS_PAYMENT_PENDING)

	def test_returns_completed_when_only_so_refs_fully_advance_paid(self):
		doc = _pe([_ref("Sales Order", "SO-001")])
		gv = _gv_factory(
			wave_order_ids={("Sales Order", "SO-001"): WAVE_ID_A},
			so_totals={"SO-001": {"grand_total": 100.0, "advance_paid": 100.0}},
		)
		with patch.object(frappe.db, "get_value", side_effect=gv):
			result = payment_status_resolver.resolve_status_for_wave_order(doc, WAVE_ID_A)
		self.assertEqual(result, payment_status_resolver.STATUS_COMPLETED)

	def test_returns_payment_pending_when_so_advance_short(self):
		doc = _pe([_ref("Sales Order", "SO-001")])
		gv = _gv_factory(
			wave_order_ids={("Sales Order", "SO-001"): WAVE_ID_A},
			so_totals={"SO-001": {"grand_total": 100.0, "advance_paid": 25.0}},
		)
		with patch.object(frappe.db, "get_value", side_effect=gv):
			result = payment_status_resolver.resolve_status_for_wave_order(doc, WAVE_ID_A)
		self.assertEqual(result, payment_status_resolver.STATUS_PAYMENT_PENDING)

	def test_mixed_si_and_so_refs_require_both_legs_settled(self):
		"""SI fully paid but SO advance short => still PAYMENT_PENDING."""
		doc = _pe([_ref("Sales Invoice", "SI-001"), _ref("Sales Order", "SO-001")])
		gv = _gv_factory(
			wave_order_ids={
				("Sales Invoice", "SI-001"): WAVE_ID_A,
				("Sales Order", "SO-001"): WAVE_ID_A,
			},
			si_outstanding={"SI-001": 0.0},
			so_totals={"SO-001": {"grand_total": 100.0, "advance_paid": 0.0}},
		)
		with patch.object(frappe.db, "get_value", side_effect=gv):
			result = payment_status_resolver.resolve_status_for_wave_order(doc, WAVE_ID_A)
		self.assertEqual(result, payment_status_resolver.STATUS_PAYMENT_PENDING)

	def test_returns_payment_pending_when_no_matching_refs_for_wave_order(self):
		"""PE has refs but none target the queried wave_order_id -> PAYMENT_PENDING (conservative)."""
		doc = _pe([_ref("Sales Invoice", "SI-001")])
		gv = _gv_factory(
			wave_order_ids={("Sales Invoice", "SI-001"): WAVE_ID_B},
			si_outstanding={"SI-001": 0.0},
		)
		with patch.object(frappe.db, "get_value", side_effect=gv):
			result = payment_status_resolver.resolve_status_for_wave_order(doc, WAVE_ID_A)
		self.assertEqual(result, payment_status_resolver.STATUS_PAYMENT_PENDING)

	def test_filters_refs_to_only_those_carrying_the_target_wave_order_id(self):
		"""Mixed-target PE: only the wave_order_id under test is consulted."""
		doc = _pe([
			_ref("Sales Invoice", "SI-A"),  # wave-A, outstanding=0
			_ref("Sales Invoice", "SI-B"),  # wave-B, outstanding=99 (irrelevant for wave-A)
		])
		gv = _gv_factory(
			wave_order_ids={
				("Sales Invoice", "SI-A"): WAVE_ID_A,
				("Sales Invoice", "SI-B"): WAVE_ID_B,
			},
			si_outstanding={"SI-A": 0.0, "SI-B": 99.0},
		)
		with patch.object(frappe.db, "get_value", side_effect=gv):
			# wave-A is fully settled (only SI-A counts).
			self.assertEqual(
				payment_status_resolver.resolve_status_for_wave_order(doc, WAVE_ID_A),
				payment_status_resolver.STATUS_COMPLETED,
			)
			# wave-B has the outstanding SI-B.
			self.assertEqual(
				payment_status_resolver.resolve_status_for_wave_order(doc, WAVE_ID_B),
				payment_status_resolver.STATUS_PAYMENT_PENDING,
			)
