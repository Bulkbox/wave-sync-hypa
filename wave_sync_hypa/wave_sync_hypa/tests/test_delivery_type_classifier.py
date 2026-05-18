"""Unit tests for the SHIPPING_COST-based delivery/pickup classifier at intake.

`_classify_delivery_type` inspects payload.fees and returns 'Delivery' when any
SHIPPING_COST fee carries a positive amount, else 'Pickup'. The value is
stamped on the SO at intake (wave_delivery_type) and read later by the
Delivery Note autopopulate hook.
"""

from __future__ import annotations

from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers.order_create import _classify_delivery_type


class TestClassifyDeliveryType(FrappeTestCase):
	"""Six branches: positive shipping / zero shipping / no shipping / other fees / typed-amount / null amount."""

	def test_positive_shipping_cost_classifies_as_delivery(self):
		payload = {"fees": [{"type": "SHIPPING_COST", "amount": 20000}]}
		self.assertEqual(_classify_delivery_type(payload), "Delivery")

	def test_zero_shipping_cost_classifies_as_pickup(self):
		payload = {"fees": [{"type": "SHIPPING_COST", "amount": 0}]}
		self.assertEqual(_classify_delivery_type(payload), "Pickup")

	def test_no_shipping_cost_fee_classifies_as_pickup(self):
		payload = {"fees": [{"type": "PLASTIC_BAGS", "amount": 500}]}
		self.assertEqual(_classify_delivery_type(payload), "Pickup")

	def test_empty_fees_classifies_as_pickup(self):
		self.assertEqual(_classify_delivery_type({"fees": []}), "Pickup")
		self.assertEqual(_classify_delivery_type({}), "Pickup")

	def test_string_amount_is_coerced_to_float(self):
		payload = {"fees": [{"type": "SHIPPING_COST", "amount": "20000"}]}
		self.assertEqual(_classify_delivery_type(payload), "Delivery")

	def test_null_or_malformed_amount_treated_as_zero(self):
		for bad in (None, "not-a-number", []):
			payload = {"fees": [{"type": "SHIPPING_COST", "amount": bad}]}
			self.assertEqual(_classify_delivery_type(payload), "Pickup", f"amount={bad!r}")

	def test_multiple_shipping_costs_one_positive_is_delivery(self):
		payload = {
			"fees": [
				{"type": "SHIPPING_COST", "amount": 0},
				{"type": "PLASTIC_BAGS", "amount": 100},
				{"type": "SHIPPING_COST", "amount": 15000},
			],
		}
		self.assertEqual(_classify_delivery_type(payload), "Delivery")

	def test_case_insensitive_fee_type_match(self):
		"""Defensive: Wave hasn't been known to deviate from upper-case, but match anyway."""
		payload = {"fees": [{"type": "shipping_cost", "amount": 10000}]}
		self.assertEqual(_classify_delivery_type(payload), "Delivery")
