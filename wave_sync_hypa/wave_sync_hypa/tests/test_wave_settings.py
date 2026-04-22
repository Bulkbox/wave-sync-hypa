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

	def test_inbound_api_key_must_be_32_chars_short_rejected(self):
		"""A key shorter than 32 characters is rejected — weak entropy, off the integration standard."""
		self.settings.enabled = 1
		self.settings.inbound_api_key = "x" * 20
		with self.assertRaises(frappe.ValidationError):
			self.settings.save(ignore_permissions=True)

	def test_inbound_api_key_must_be_32_chars_long_rejected(self):
		"""A key longer than 32 characters is rejected — usually a paste mistake."""
		self.settings.enabled = 1
		self.settings.inbound_api_key = "x" * 40
		with self.assertRaises(frappe.ValidationError):
			self.settings.save(ignore_permissions=True)

	def test_inbound_api_key_32_chars_accepted(self):
		"""A key that is exactly 32 characters long saves successfully."""
		self.settings.enabled = 1
		self.settings.inbound_api_key = "A" * 32
		# Should not raise.
		self.settings.save(ignore_permissions=True)
		# Confirm the stored password round-trips.
		reloaded = frappe.get_single("Wave Settings")
		self.assertEqual(reloaded.get_password("inbound_api_key", raise_exception=False), "A" * 32)

	def test_inbound_api_key_non_ascii_character_rejected(self):
		"""A 32-char key containing £ (non-ASCII) is rejected — HTTP header encoding is ambiguous."""
		self.settings.enabled = 1
		self.settings.inbound_api_key = "g%be4EFKU/7]_rEz/<<s£Aj21'$&RU:N"  # real-world broken key
		with self.assertRaises(frappe.ValidationError):
			self.settings.save(ignore_permissions=True)

	def test_inbound_api_key_shell_special_chars_rejected(self):
		"""A 32-char key containing $, &, ' breaks shell-quoted curls; reject at save time."""
		self.settings.enabled = 1
		self.settings.inbound_api_key = "abc" + "$" * 4 + "def" + "&" * 4 + "ghi" + "'" * 4 + "AAAAAAAAAAA"
		with self.assertRaises(frappe.ValidationError):
			self.settings.save(ignore_permissions=True)

	def test_inbound_api_key_slash_plus_equals_rejected(self):
		"""Standard base64 alphabet (with /, +, =) is NOT URL-safe; reject."""
		self.settings.enabled = 1
		self.settings.inbound_api_key = "a" * 30 + "/="
		with self.assertRaises(frappe.ValidationError):
			self.settings.save(ignore_permissions=True)

	def test_inbound_api_key_url_safe_accepted(self):
		"""The full URL-safe charset (letters, digits, underscore, hyphen) saves cleanly."""
		self.settings.enabled = 1
		self.settings.inbound_api_key = "A-B_c" + "d" * 27  # 32 chars, URL-safe
		self.settings.save(ignore_permissions=True)

	def test_masked_inbound_key_does_not_retrigger_length_check(self):
		"""Re-saving without touching the Password field (value comes in as a mask) must not raise."""
		self.settings.enabled = 1
		self.settings.inbound_api_key = "B" * 32
		self.settings.save(ignore_permissions=True)
		# Simulate the UI behaviour: reload and save again with the masked value.
		reloaded = frappe.get_single("Wave Settings")
		reloaded.inbound_api_key = "*" * 12   # shorter than 32 but fully masked
		# Should not raise — a masked value means "no change to the stored secret".
		reloaded.save(ignore_permissions=True)
