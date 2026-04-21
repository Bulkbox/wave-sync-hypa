"""Unit tests for the Wave Settings controller."""

import frappe
from frappe.tests.utils import FrappeTestCase


class TestWaveSettings(FrappeTestCase):
	"""Invariants enforced by WaveSettings.validate."""

	def setUp(self):
		"""Snapshot current values via direct DB reads and set a known-good baseline."""
		self._baseline = {
			"enabled": frappe.db.get_single_value("Wave Settings", "enabled") or 0,
			"price_scale_divisor": frappe.db.get_single_value(
				"Wave Settings", "price_scale_divisor"
			) or 100,
			"log_retention_days": frappe.db.get_single_value(
				"Wave Settings", "log_retention_days"
			) or 14,
		}
		self._write_fields(enabled=0, price_scale_divisor=100, log_retention_days=14)
		self.settings = frappe.get_single("Wave Settings")

	def tearDown(self):
		"""Restore the snapshot directly via DB writes; validate() is exercised by the tests, not fixtures."""
		self._write_fields(**self._baseline)

	def _write_fields(self, **fields) -> None:
		"""Persist Wave Settings fields via direct DB writes to bypass validate() in fixture lifecycle."""
		for name, value in fields.items():
			frappe.db.set_value("Wave Settings", "Wave Settings", name, value, update_modified=False)
		frappe.db.commit()

	def test_price_scale_divisor_must_be_positive(self):
		"""Zero or negative divisors would break cents-to-major conversion and are rejected."""
		self.settings.price_scale_divisor = 0
		with self.assertRaises(frappe.ValidationError):
			self.settings.save(ignore_permissions=True)

	def test_log_retention_days_must_be_positive(self):
		"""A zero retention window would delete every row immediately and is rejected."""
		self.settings.log_retention_days = 0
		with self.assertRaises(frappe.ValidationError):
			self.settings.save(ignore_permissions=True)

	def test_enabling_without_inbound_key_is_rejected(self):
		"""Turning the integration on requires an inbound API key; otherwise anyone could post."""
		self.settings.enabled = 1
		self.settings.inbound_api_key = ""
		with self.assertRaises(frappe.ValidationError):
			self.settings.save(ignore_permissions=True)
