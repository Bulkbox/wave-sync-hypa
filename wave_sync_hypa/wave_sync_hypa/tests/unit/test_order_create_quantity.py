"""Unit tests for purchase-step (stepToUom) handling in order intake.

Wave's order-line `quantity` is a count of purchase steps; the SO line qty
must be quantity x stepToUom. These cover the multiply, the step=1 no-op,
missing-field defaults, and the audit row that surfaces stepped lines — all
at the pure-function boundary (no DB).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers import order_create


class TestLineUnits(FrappeTestCase):
	def test_step_one_leaves_quantity_unchanged(self):
		self.assertEqual(order_create._line_units({"quantity": 3, "stepToUom": 1}), 3)

	def test_step_multiplies_quantity(self):
		self.assertEqual(order_create._line_units({"quantity": 1, "stepToUom": 5}), 5)
		self.assertEqual(order_create._line_units({"quantity": 2, "stepToUom": 6}), 12)

	def test_missing_step_defaults_to_one(self):
		self.assertEqual(order_create._line_units({"quantity": 4}), 4)

	def test_missing_quantity_defaults_to_one(self):
		self.assertEqual(order_create._line_units({"stepToUom": 5}), 5)

	def test_build_item_line_applies_step(self):
		so = SimpleNamespace(delivery_date="2026-06-10")
		line = order_create._build_item_line("ITEM-1", {"quantity": 2, "stepToUom": 5}, so)
		self.assertEqual(line["item_code"], "ITEM-1")
		self.assertEqual(line["qty"], 10)
		self.assertEqual(line["delivery_date"], "2026-06-10")

	def test_unresolved_entry_reports_true_units(self):
		entry = order_create._unresolved_product_entry(
			"SKU-1", {"quantity": 2, "stepToUom": 5, "productId": "p1"}, Exception("nope")
		)
		self.assertEqual(entry["quantity"], 10)
		self.assertEqual(entry["sku"], "SKU-1")


class TestResolvedItemsAudit(FrappeTestCase):
	def test_audit_surfaces_only_stepped_lines(self):
		payload = {
			"_id": "o1",
			"products": [
				{"sku": "A", "quantity": 1, "stepToUom": 5},
				{"sku": "B", "quantity": 2, "stepToUom": 1},
			],
		}
		with patch.object(order_create, "log_step") as mock_log:
			order_create._log_items_resolved("corr", payload, 2)
		body = mock_log.call_args.kwargs["response_body"]
		self.assertEqual(body["product_line_count"], 2)
		self.assertEqual(body["purchase_step_applied"], [{"sku": "A", "quantity": 1, "step": 5, "units": 5}])

	def test_audit_omits_step_key_when_no_steps(self):
		payload = {"_id": "o1", "products": [{"sku": "B", "quantity": 2, "stepToUom": 1}]}
		with patch.object(order_create, "log_step") as mock_log:
			order_create._log_items_resolved("corr", payload, 1)
		self.assertNotIn("purchase_step_applied", mock_log.call_args.kwargs["response_body"])
