"""Unit tests for handlers.delivery_note.autopopulate_from_wave_so.

before_insert hook: pull delivery_date + (pickup) driver from the first
linked Wave Sales Order. The matrix:

  1. No Wave SO linked -> no-op
  2. Non-Wave SO linked -> no-op
  3. Wave SO, type=Delivery -> delivery_date copied, driver untouched
  4. Wave SO, type=Pickup, pickup_driver set -> both stamped
  5. Wave SO, type=Pickup, pickup_driver blank -> only delivery_date stamped
  6. Multi-SO with conflicting types/dates -> first SO wins + Warning row
  7. Operator pre-filled driver -> we respect it (don't overwrite)
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers import delivery_note as dn_handler


def _dn(items: list | None = None, driver: str = "") -> SimpleNamespace:
	"""DN stand-in carrying just the surface autopopulate_from_wave_so touches."""
	doc = SimpleNamespace(
		doctype="Delivery Note",
		name="DN-2026-0001",
		delivery_date=None,
		driver=driver,
	)
	values = {"items": items or [], "wave_order_id": ""}
	doc.get = lambda key, default=None: values.get(key, default)
	return doc


def _item(against_sales_order: str = "") -> dict:
	"""DN item-row stand-in carrying just against_sales_order."""
	return {"against_sales_order": against_sales_order}


def _settings(pickup_driver: str = "") -> MagicMock:
	"""Wave Settings stand-in carrying the configured pickup driver (or blank)."""
	settings = MagicMock(name="WaveSettings")
	settings.get.side_effect = lambda key, default=None: {
		"wave_pickup_driver": pickup_driver,
	}.get(key, default)
	return settings


def _so_row(name: str = "SAL-ORD-001", delivery_date=date(2026, 5, 20), delivery_type: str = "Delivery") -> dict:
	"""Row matching what _read_wave_so returns (as_dict=True)."""
	return {"name": name, "delivery_date": delivery_date, "wave_delivery_type": delivery_type}


class TestAutopopulateFromWaveSo(FrappeTestCase):
	"""before_insert hook: 7 cases covering every guard clause + the mutating block."""

	def test_no_items_is_noop(self):
		doc = _dn(items=[])
		with (
			patch.object(frappe.db, "get_value") as mock_get_value,
			patch.object(frappe, "get_cached_doc") as mock_settings,
			patch.object(dn_handler, "log_step") as mock_log,
		):
			dn_handler.autopopulate_from_wave_so(doc)
		self.assertIsNone(doc.delivery_date)
		self.assertEqual(doc.driver, "")
		# Never read DB or settings, never logged.
		mock_get_value.assert_not_called()
		mock_settings.assert_not_called()
		mock_log.assert_not_called()

	def test_non_wave_so_is_noop(self):
		"""Linked SO exists but has no wave_order_id -> _collect_distinct_wave_order_ids returns []."""
		doc = _dn(items=[_item("SAL-ORD-NON-WAVE")])
		with (
			patch.object(frappe.db, "get_value", return_value=""),  # wave_order_id lookup returns empty
			patch.object(frappe, "get_cached_doc") as mock_settings,
			patch.object(dn_handler, "log_step") as mock_log,
		):
			dn_handler.autopopulate_from_wave_so(doc)
		self.assertIsNone(doc.delivery_date)
		self.assertEqual(doc.driver, "")
		mock_settings.assert_not_called()
		mock_log.assert_not_called()

	def test_delivery_type_copies_date_leaves_driver(self):
		"""SO with wave_delivery_type='Delivery' -> stamp delivery_date, driver untouched."""
		doc = _dn(items=[_item("SAL-ORD-001")])
		so_row = _so_row(delivery_type="Delivery")

		def _get_value_dispatch(*args, **kwargs):
			if args[0] == "Sales Order" and args[1] == "SAL-ORD-001":
				# First call: wave_order_id lookup by SO name (from _collect_distinct_wave_order_ids).
				return "wave-id-AAA"
			# Second call: _read_wave_so by wave_order_id -> as_dict row
			return so_row

		with (
			patch.object(frappe.db, "get_value", side_effect=_get_value_dispatch),
			patch.object(frappe, "get_cached_doc") as mock_settings,
			patch.object(dn_handler, "log_step") as mock_log,
		):
			dn_handler.autopopulate_from_wave_so(doc)
		self.assertEqual(doc.delivery_date, date(2026, 5, 20))
		self.assertEqual(doc.driver, "")
		# Settings never consulted for Delivery type.
		mock_settings.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(dn_handler.STEP_AUTOPOPULATED, steps)

	def test_pickup_type_with_configured_driver_stamps_both(self):
		doc = _dn(items=[_item("SAL-ORD-002")])
		so_row = _so_row(delivery_type="Pickup")

		def _get_value_dispatch(*args, **kwargs):
			if args[0] == "Sales Order" and args[1] == "SAL-ORD-002":
				return "wave-id-BBB"
			return so_row

		with (
			patch.object(frappe.db, "get_value", side_effect=_get_value_dispatch),
			patch.object(frappe, "get_cached_doc", return_value=_settings(pickup_driver="DRV-PICKUP")),
			patch.object(dn_handler, "log_step") as mock_log,
		):
			dn_handler.autopopulate_from_wave_so(doc)
		self.assertEqual(doc.delivery_date, date(2026, 5, 20))
		self.assertEqual(doc.driver, "DRV-PICKUP")
		audit = [c for c in mock_log.call_args_list if c.kwargs.get("step") == dn_handler.STEP_AUTOPOPULATED]
		self.assertEqual(len(audit), 1)
		self.assertEqual(audit[0].kwargs.get("request_body", {}).get("autopopulated", {}).get("driver"), "DRV-PICKUP")

	def test_pickup_type_with_blank_driver_setting_leaves_driver_empty(self):
		doc = _dn(items=[_item("SAL-ORD-003")])
		so_row = _so_row(delivery_type="Pickup")

		def _get_value_dispatch(*args, **kwargs):
			if args[0] == "Sales Order" and args[1] == "SAL-ORD-003":
				return "wave-id-CCC"
			return so_row

		with (
			patch.object(frappe.db, "get_value", side_effect=_get_value_dispatch),
			patch.object(frappe, "get_cached_doc", return_value=_settings(pickup_driver="")),
			patch.object(dn_handler, "log_step"),
		):
			dn_handler.autopopulate_from_wave_so(doc)
		self.assertEqual(doc.delivery_date, date(2026, 5, 20))
		self.assertEqual(doc.driver, "")

	def test_operator_prefilled_driver_is_preserved(self):
		"""DN already carries a driver at before_insert -> we don't overwrite even for pickup."""
		doc = _dn(items=[_item("SAL-ORD-004")], driver="DRV-MANUAL")
		so_row = _so_row(delivery_type="Pickup")

		def _get_value_dispatch(*args, **kwargs):
			if args[0] == "Sales Order" and args[1] == "SAL-ORD-004":
				return "wave-id-DDD"
			return so_row

		with (
			patch.object(frappe.db, "get_value", side_effect=_get_value_dispatch),
			patch.object(frappe, "get_cached_doc", return_value=_settings(pickup_driver="DRV-PICKUP")),
			patch.object(dn_handler, "log_step"),
		):
			dn_handler.autopopulate_from_wave_so(doc)
		# Driver respected; only delivery_date stamped.
		self.assertEqual(doc.driver, "DRV-MANUAL")
		self.assertEqual(doc.delivery_date, date(2026, 5, 20))

	def test_multi_so_conflicting_values_logs_warning_uses_first(self):
		"""Two Wave SOs with different delivery_date -> Warning row, first SO wins."""
		doc = _dn(items=[_item("SAL-ORD-005"), _item("SAL-ORD-006")])
		primary = _so_row(name="SAL-ORD-005", delivery_date=date(2026, 5, 20), delivery_type="Delivery")
		secondary = _so_row(name="SAL-ORD-006", delivery_date=date(2026, 5, 21), delivery_type="Pickup")

		def _get_value_dispatch(*args, **kwargs):
			# _collect_distinct_wave_order_ids does two SO->wave_order_id reads.
			if args[0] == "Sales Order" and args[1] == "SAL-ORD-005" and args[2] == "wave_order_id":
				return "wave-id-PRIMARY"
			if args[0] == "Sales Order" and args[1] == "SAL-ORD-006" and args[2] == "wave_order_id":
				return "wave-id-SECOND"
			# _read_wave_so dispatch — keyed by filter dict's wave_order_id.
			filters = args[1] if isinstance(args[1], dict) else {}
			if filters.get("wave_order_id") == "wave-id-PRIMARY":
				return primary
			if filters.get("wave_order_id") == "wave-id-SECOND":
				return secondary
			return None

		with (
			patch.object(frappe.db, "get_value", side_effect=_get_value_dispatch),
			patch.object(frappe, "get_cached_doc") as mock_settings,
			patch.object(dn_handler, "log_step") as mock_log,
		):
			dn_handler.autopopulate_from_wave_so(doc)
		# First SO's values used.
		self.assertEqual(doc.delivery_date, date(2026, 5, 20))
		self.assertEqual(doc.driver, "")  # primary is Delivery type
		# Settings never consulted because primary was Delivery type.
		mock_settings.assert_not_called()
		# Both audit rows present: heterogeneous Warning + autopopulated Info.
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(dn_handler.STEP_AUTOPOPULATE_HETEROGENEOUS, steps)
		self.assertIn(dn_handler.STEP_AUTOPOPULATED, steps)
