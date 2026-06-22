"""Unit tests for resilient customer resolution + delivery-date clamp in order_create (issue #142)."""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import getdate

from wave_sync_hypa.wave_sync_hypa.handlers import order_create
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveValidationError


def _payload(**overrides) -> dict:
	base = {"_id": "WID", "friendlyId": "100", "user": {"_id": "U1", "firstName": "A"}}
	base.update(overrides)
	return base


class TestCustomerResolutionResilience(FrappeTestCase):
	"""A customer-side problem must route to walk-in, never drop the order."""

	def test_missing_user_id_falls_back_to_walk_in(self):
		with (
			patch.object(frappe.db, "get_single_value", return_value="Walk In"),
			patch.object(order_create, "log_step") as mock_log,
		):
			result = order_create._resolve_customer_for_order(_payload(user={}), "c")
		self.assertEqual(result, "Walk In")
		mock_log.assert_called_once()

	def test_create_failure_falls_back_to_walk_in(self):
		with (
			patch.object(order_create, "find_or_create_customer", side_effect=RuntimeError("slade tax_id")),
			patch.object(frappe.db, "get_single_value", return_value="Walk In"),
			patch.object(order_create, "log_step"),
		):
			result = order_create._resolve_customer_for_order(_payload(), "c")
		self.assertEqual(result, "Walk In")

	def test_disabled_customer_is_reenabled(self):
		with (
			patch.object(order_create, "find_or_create_customer", return_value=("CUST", False, "wave_id")),
			patch.object(frappe.db, "get_value", return_value=1),
			patch.object(frappe.db, "set_value") as mock_set,
			patch.object(order_create, "log_step"),
		):
			result = order_create._resolve_customer_for_order(_payload(), "c")
		self.assertEqual(result, "CUST")
		args = mock_set.call_args.args
		self.assertEqual((args[0], args[2], args[3]), ("Customer", "disabled", 0))

	def test_no_walk_in_configured_raises(self):
		with patch.object(frappe.db, "get_single_value", return_value=None):
			with self.assertRaises(WaveValidationError):
				order_create._resolve_customer_for_order(_payload(user={}), "c")


class TestDeliveryDateClamp(FrappeTestCase):
	"""delivery_date must never precede transaction_date (ERPNext rejects that)."""

	def test_delivery_date_clamped_to_transaction_date(self):
		settings = SimpleNamespace(
			default_company="C", default_currency="KES", default_price_list="PL", default_warehouse="WH",
			price_scale_divisor=100,
		)
		payload = {
			"_id": "W", "friendlyId": "1", "status": "PENDING", "deliveryService": "standard",
			"createdAt": "2026-06-03", "timeSlotStart": "2026-06-01",
		}
		so = order_create._build_sales_order_header(settings, "CUST", None, payload, "c")
		self.assertEqual(getdate(so.delivery_date), getdate(so.transaction_date))
