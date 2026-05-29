"""Unit tests for the delivery/pickup classifier at intake.

`_classify_delivery_type` reads Wave's `deliveryService` field — `takeAway`
means pickup, any other non-empty value means delivery. When that field is
missing (legacy payloads) it falls back to address.street presence.
"""

from __future__ import annotations

from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers.order_create import _classify_delivery_type


class TestClassifyDeliveryType(FrappeTestCase):
	"""deliveryService primary signal; address.street fallback."""

	def test_take_away_classifies_as_pickup(self):
		self.assertEqual(_classify_delivery_type({"deliveryService": "takeAway"}), "Pickup")

	def test_take_away_case_insensitive(self):
		self.assertEqual(_classify_delivery_type({"deliveryService": "TAKEAWAY"}), "Pickup")
		self.assertEqual(_classify_delivery_type({"deliveryService": "takeaway"}), "Pickup")

	def test_standard_classifies_as_delivery(self):
		self.assertEqual(_classify_delivery_type({"deliveryService": "standard"}), "Delivery")

	def test_express_classifies_as_delivery(self):
		self.assertEqual(_classify_delivery_type({"deliveryService": "express"}), "Delivery")

	def test_unknown_non_takeaway_service_classifies_as_delivery(self):
		"""Any non-empty, non-takeAway value is delivery — Wave may add new services."""
		self.assertEqual(_classify_delivery_type({"deliveryService": "scheduled"}), "Delivery")

	def test_missing_service_with_address_falls_back_to_delivery(self):
		payload = {"address": {"street": "Muthithi Road", "streetNo": "0010"}}
		self.assertEqual(_classify_delivery_type(payload), "Delivery")

	def test_missing_service_without_address_falls_back_to_pickup(self):
		self.assertEqual(_classify_delivery_type({}), "Pickup")
		self.assertEqual(_classify_delivery_type({"address": {}}), "Pickup")
		self.assertEqual(_classify_delivery_type({"address": None}), "Pickup")

	def test_empty_service_with_blank_address_street_is_pickup(self):
		"""Address present but no street -> pickup (e.g. dropoff metadata only)."""
		payload = {"deliveryService": "", "address": {"street": ""}}
		self.assertEqual(_classify_delivery_type(payload), "Pickup")
