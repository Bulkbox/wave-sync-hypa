"""Unit tests for the tax-aware back-calc helpers on fee lines.

Confirms:
  * The math: rate + tax == Wave amount, for several tax rates.
  * The opt-in semantics: blank template setting → no back-calc.
  * The fee-type filter: only SHIPPING_COST gets back-calculated (today).
  * The line shape: per-line item_tax_template only stamped when configured.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers import order_create as oc


def _settings(*, template: str = "", divisor: int = 100) -> MagicMock:
	"""Wave Settings stand-in carrying the per-test divisor + shipping template."""
	settings = MagicMock(name="WaveSettings")
	settings.price_scale_divisor = divisor
	settings.get.side_effect = lambda key, default=None: {
		"shipping_item_tax_template": template,
	}.get(key, default)
	return settings


def _so() -> SimpleNamespace:
	"""SO stub with a delivery_date the fee line copies onto its row."""
	return SimpleNamespace(delivery_date="2026-05-16")


class TestShippingLineRate(FrappeTestCase):
	"""The arithmetic: rate * (1 + T/100) == Wave amount."""

	def test_zero_template_yields_unchanged_amount(self):
		self.assertEqual(oc._shipping_line_rate(200.0, ""), 200.0)

	def test_template_with_zero_total_rate_yields_unchanged_amount(self):
		with patch.object(frappe.db, "get_all", return_value=[{"tax_rate": 0}]):
			self.assertEqual(oc._shipping_line_rate(200.0, "Zero Rated"), 200.0)

	def test_template_with_16_percent_back_calculates(self):
		with patch.object(frappe.db, "get_all", return_value=[{"tax_rate": 16}]):
			rate = oc._shipping_line_rate(200.0, "VAT 16")
		self.assertAlmostEqual(rate, 200.0 / 1.16, places=6)
		# Critical guarantee: rate + tax = Wave amount.
		self.assertAlmostEqual(rate + rate * 0.16, 200.0, places=6)

	def test_template_with_compound_rates_sums_them(self):
		# VAT 16% + Tourism Levy 1.5% → effective 17.5%
		with patch.object(frappe.db, "get_all", return_value=[
			{"tax_rate": 16}, {"tax_rate": 1.5},
		]):
			rate = oc._shipping_line_rate(200.0, "VAT + Tourism")
		self.assertAlmostEqual(rate, 200.0 / 1.175, places=6)

	def test_template_with_only_negative_rate_falls_back_to_amount(self):
		"""Defensive: negative or nonsense rates should not produce a weird rate."""
		with patch.object(frappe.db, "get_all", return_value=[{"tax_rate": -5}]):
			self.assertEqual(oc._shipping_line_rate(200.0, "Bad Template"), 200.0)


class TestFeeLineItemTaxTemplate(FrappeTestCase):
	"""Only SHIPPING_COST triggers the lookup today; other fee types return empty."""

	def test_shipping_cost_returns_configured_template(self):
		settings = _settings(template="VAT 16")
		self.assertEqual(oc._fee_line_item_tax_template(settings, "SHIPPING_COST"), "VAT 16")

	def test_shipping_cost_with_no_template_configured_returns_empty(self):
		settings = _settings(template="")
		self.assertEqual(oc._fee_line_item_tax_template(settings, "SHIPPING_COST"), "")

	def test_other_fee_types_never_get_back_calc(self):
		settings = _settings(template="VAT 16")
		self.assertEqual(oc._fee_line_item_tax_template(settings, "PLASTIC_BAGS"), "")
		self.assertEqual(oc._fee_line_item_tax_template(settings, ""), "")
		self.assertEqual(oc._fee_line_item_tax_template(settings, None), "")


class TestBuildFeeLine(FrappeTestCase):
	"""The dict shape we hand to sales_order.append('items', ...)."""

	def test_shipping_with_template_carries_per_line_override(self):
		settings = _settings(template="VAT 16")
		with patch.object(frappe.db, "get_all", return_value=[{"tax_rate": 16}]):
			line = oc._build_fee_line("Shipping Cost", "SHIPPING_COST", 200.0, _so(), settings)
		self.assertEqual(line["item_code"], "Shipping Cost")
		self.assertAlmostEqual(line["rate"], 200.0 / 1.16, places=6)
		self.assertEqual(line["item_tax_template"], "VAT 16")
		self.assertEqual(line["qty"], 1)

	def test_shipping_without_template_omits_per_line_override(self):
		settings = _settings(template="")
		line = oc._build_fee_line("Shipping Cost", "SHIPPING_COST", 200.0, _so(), settings)
		self.assertEqual(line["rate"], 200.0)
		self.assertNotIn("item_tax_template", line)

	def test_non_shipping_fee_never_back_calculates(self):
		"""Plastic bags + VAT 16 template configured for shipping → bags still get raw amount."""
		settings = _settings(template="VAT 16")
		line = oc._build_fee_line("Plastic Bag Fee", "PLASTIC_BAGS", 10.0, _so(), settings)
		self.assertEqual(line["rate"], 10.0)
		self.assertNotIn("item_tax_template", line)
