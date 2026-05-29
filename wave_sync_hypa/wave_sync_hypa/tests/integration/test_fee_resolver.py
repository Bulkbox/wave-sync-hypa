"""Unit tests for resolvers.fee_resolver.resolve_fee."""

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.resolvers.fee_resolver import resolve_fee
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveResolutionError


class TestResolveFee(FrappeTestCase):
	"""Fee mappings live in Wave Settings.fee_mappings; resolver reads them at call time."""

	TEST_FEE_TYPE = "WAVE_SYNC_TEST_FEE"

	def setUp(self):
		"""Pick any existing Item to map to and seed a one-off fee mapping row."""
		self.probe_item = frappe.db.get_value("Item", {"disabled": 0}, "item_code")
		if not self.probe_item:
			self.skipTest("No Items installed on the site; cannot run fee resolution tests.")
		self._original_rows = self._snapshot_mappings()
		self._clear_mappings()
		self._add_mapping(self.TEST_FEE_TYPE, self.probe_item)

	def tearDown(self):
		"""Restore the original mapping rows."""
		self._clear_mappings()
		self._restore_mappings(self._original_rows)

	def _snapshot_mappings(self) -> list[dict]:
		"""Capture current fee_mappings so we can restore them after the test."""
		settings = frappe.get_single("Wave Settings")
		return [
			{
				"wave_fee_type": row.wave_fee_type,
				"erp_item_code": row.erp_item_code,
				"description": row.description,
			}
			for row in (settings.fee_mappings or [])
		]

	def _clear_mappings(self) -> None:
		"""Remove every fee_mappings row via direct DB writes."""
		frappe.db.delete("Wave Fee Mapping", {"parent": "Wave Settings"})
		frappe.db.commit()
		frappe.clear_document_cache("Wave Settings", "Wave Settings")

	def _add_mapping(self, fee_type: str, item_code: str) -> None:
		"""Append one Wave Fee Mapping row and save without the single's validate pipeline."""
		settings = frappe.get_single("Wave Settings")
		settings.append(
			"fee_mappings",
			{"wave_fee_type": fee_type, "erp_item_code": item_code, "description": "test"},
		)
		settings.flags.ignore_validate = True
		settings.save(ignore_permissions=True)
		frappe.clear_document_cache("Wave Settings", "Wave Settings")

	def _restore_mappings(self, rows: list[dict]) -> None:
		"""Reinstate the originally snapshotted fee_mappings rows."""
		settings = frappe.get_single("Wave Settings")
		settings.fee_mappings = []
		for row in rows:
			settings.append("fee_mappings", row)
		settings.flags.ignore_validate = True
		settings.save(ignore_permissions=True)
		frappe.clear_document_cache("Wave Settings", "Wave Settings")

	def test_returns_item_code_for_known_fee(self):
		"""A mapped fee type returns the configured Item."""
		self.assertEqual(resolve_fee(self.TEST_FEE_TYPE), self.probe_item)

	def test_raises_for_empty_fee_type(self):
		"""An empty fee type is malformed and raises."""
		with self.assertRaises(WaveResolutionError):
			resolve_fee("")

	def test_raises_for_unmapped_fee_type(self):
		"""A fee type Wave emits that no mapping covers raises a resolution error."""
		with self.assertRaises(WaveResolutionError):
			resolve_fee("UNMAPPED_FEE_TYPE")
