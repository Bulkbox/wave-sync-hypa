"""Unit tests for resolvers.item_resolver.resolve_sku."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.resolvers.item_resolver import resolve_sku
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveResolutionError


def _item(**overrides):
	base = {"name": "X", "disabled": 0, "has_variants": 0, "is_sales_item": 1}
	base.update(overrides)
	return frappe._dict(base)


class TestResolveSku(FrappeTestCase):
	"""Return the Item code when sellable; raise WaveResolutionError otherwise."""

	def setUp(self):
		"""Locate a sellable Item (enabled, non-template, sales item) as the positive fixture."""
		self.existing_item = frappe.db.get_value(
			"Item", {"disabled": 0, "has_variants": 0, "is_sales_item": 1}, "item_code"
		)
		if not self.existing_item:
			self.skipTest("No sellable Items installed on the site; cannot run SKU resolution tests.")

	def test_returns_item_code_when_sku_exists(self):
		"""A known SKU resolves to its ERP Item name."""
		self.assertEqual(resolve_sku(self.existing_item), self.existing_item)

	def test_raises_when_sku_is_none(self):
		"""A missing SKU is a malformed payload and must not silently succeed."""
		with self.assertRaises(WaveResolutionError):
			resolve_sku(None)

	def test_raises_when_sku_is_empty_string(self):
		"""An empty SKU is the same as missing — reject."""
		with self.assertRaises(WaveResolutionError):
			resolve_sku("")

	def test_raises_when_sku_not_found(self):
		"""A SKU that does not exist in ERP raises a resolution error, not a DoesNotExist."""
		with self.assertRaises(WaveResolutionError):
			resolve_sku("__wave_sync_nonexistent_sku__")

	def test_raises_when_item_disabled(self):
		"""A disabled Item must be rejected so it soft-skips instead of breaking the SO insert."""
		with patch.object(frappe.db, "get_value", return_value=_item(disabled=1)):
			with self.assertRaises(WaveResolutionError):
				resolve_sku("SKU")

	def test_raises_when_item_is_template(self):
		"""A template (has_variants) Item cannot be sold directly; reject it."""
		with patch.object(frappe.db, "get_value", return_value=_item(has_variants=1)):
			with self.assertRaises(WaveResolutionError):
				resolve_sku("SKU")

	def test_raises_when_item_not_sales_item(self):
		"""An Item not marked as a sales item would fail ERPNext's selling validation; reject it."""
		with patch.object(frappe.db, "get_value", return_value=_item(is_sales_item=0)):
			with self.assertRaises(WaveResolutionError):
				resolve_sku("SKU")

	def test_returns_name_for_sellable_item(self):
		"""An enabled, non-template, sales Item resolves to its name."""
		with patch.object(frappe.db, "get_value", return_value=_item(name="ITEM-1")):
			self.assertEqual(resolve_sku("SKU"), "ITEM-1")
