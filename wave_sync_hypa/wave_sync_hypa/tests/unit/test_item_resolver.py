"""Unit tests for resolvers.item_resolver.resolve_sku."""

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.resolvers.item_resolver import resolve_sku
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveResolutionError


class TestResolveSku(FrappeTestCase):
	"""Return the Item code when present; raise WaveResolutionError otherwise."""

	def setUp(self):
		"""Locate any existing enabled Item to use as the positive fixture."""
		self.existing_item = frappe.db.get_value("Item", {"disabled": 0}, "item_code")
		if not self.existing_item:
			self.skipTest("No Items installed on the site; cannot run SKU resolution tests.")

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
