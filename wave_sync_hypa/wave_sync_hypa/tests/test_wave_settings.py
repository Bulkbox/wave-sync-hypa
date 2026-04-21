"""Unit tests for the Wave Settings controller."""

import frappe
from frappe.tests.utils import FrappeTestCase


class TestWaveSettings(FrappeTestCase):
	"""Invariants enforced by WaveSettings.validate."""

	def setUp(self):
		"""Load the Single and snapshot its baseline for restore in tearDown."""
		self.settings = frappe.get_single("Wave Settings")
		self._baseline = {
			"enabled": self.settings.enabled,
			"price_scale_divisor": self.settings.price_scale_divisor or 100,
			"log_retention_days": self.settings.log_retention_days or 14,
		}

	def tearDown(self):
		"""Restore baseline values so later tests are not affected."""
		fresh = frappe.get_single("Wave Settings")
		for field, value in self._baseline.items():
			fresh.set(field, value)
		fresh.save(ignore_permissions=True)

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
