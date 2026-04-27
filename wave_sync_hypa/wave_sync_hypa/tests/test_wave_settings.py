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
		"""Turning the integration on with NO key on file is rejected; otherwise anyone could post."""
		# Clear any leftover encrypted key so this test reflects a fresh-install state.
		frappe.db.sql(
			"DELETE FROM `__Auth` WHERE doctype='Wave Settings' AND fieldname='inbound_api_key'"
		)
		frappe.db.commit()
		self.settings = frappe.get_single("Wave Settings")
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

	def test_null_inbound_key_with_stored_value_does_not_raise(self):
		"""When the form posts inbound_api_key as None / '' but the encrypted store has a key, save still passes.

		Regression cover for the data-loss reports: child-table-only edits in
		the Desk sometimes posted the password field as null. The old validator
		treated that as 'operator cleared the key' and threw, aborting the save
		mid-flight after Frappe had already deleted the existing child rows.
		"""
		# Seed a real key first.
		self.settings.enabled = 1
		self.settings.inbound_api_key = "C" * 32
		self.settings.save(ignore_permissions=True)
		# Now simulate the form posting back with the in-memory field as None.
		reloaded = frappe.get_single("Wave Settings")
		reloaded.inbound_api_key = None
		# Must not raise — the stored value is the source of truth.
		reloaded.save(ignore_permissions=True)
		# Confirm the stored value survived.
		again = frappe.get_single("Wave Settings")
		self.assertEqual(again.get_password("inbound_api_key", raise_exception=False), "C" * 32)

	def test_empty_string_inbound_key_with_stored_value_does_not_raise(self):
		"""Same regression cover for the empty-string variant — some browsers blank password fields on POST."""
		self.settings.enabled = 1
		self.settings.inbound_api_key = "D" * 32
		self.settings.save(ignore_permissions=True)
		reloaded = frappe.get_single("Wave Settings")
		reloaded.inbound_api_key = ""
		reloaded.save(ignore_permissions=True)
		again = frappe.get_single("Wave Settings")
		self.assertEqual(again.get_password("inbound_api_key", raise_exception=False), "D" * 32)

	def test_validate_skipped_during_install_and_migrate(self):
		"""Framework-driven saves must not be blocked by operator-facing validation.

		Patches and bench migrate may write to Wave Settings via
		frappe.db.set_single_value (which bypasses validate) but also via
		patches that load the doc and save it. In the latter case, the
		in_install / in_migrate flags signal "trust the caller; skip operator
		invariants" — exactly the convention ERPNext core uses.
		"""
		# Set up a state that would normally throw: enabled=1 with no key.
		from wave_sync_hypa.wave_sync_hypa.doctype.wave_settings.wave_settings import WaveSettings

		# Wipe the stored key so validate() would fail without the in_migrate guard.
		frappe.db.sql(
			"DELETE FROM `__Auth` WHERE doctype='Wave Settings' AND fieldname='inbound_api_key'"
		)
		frappe.db.commit()

		settings = frappe.get_single("Wave Settings")
		settings.enabled = 1
		settings.inbound_api_key = None

		# Without the guard, this would throw. Set in_migrate to verify the guard.
		original = frappe.flags.in_migrate
		frappe.flags.in_migrate = True
		try:
			settings.save(ignore_permissions=True)
		finally:
			frappe.flags.in_migrate = original

		# Restore the test invariant: re-seed a key so other tests aren't affected.
		settings.inbound_api_key = "E" * 32
		settings.save(ignore_permissions=True)

	def test_post_save_audit_row_records_child_table_counts(self):
		"""Every Wave Settings save writes one Wave Sync Log row with the child-table head count.

		That row is the canonical 'what was on file' record; used to investigate
		any future 'my rules disappeared' claim with a single SQL filter.
		"""
		self.settings.enabled = 1
		self.settings.inbound_api_key = "F" * 32
		self.settings.save(ignore_permissions=True)
		audit = frappe.get_all(
			"Wave Sync Log",
			filters={"step": "settings_post_save_snapshot"},
			fields=["name", "request_body"],
			order_by="creation desc",
			limit=1,
		)
		self.assertEqual(len(audit), 1)
		self.assertIn("child_table_row_counts", audit[0].request_body)
		self.assertIn("inbound_api_key_on_file", audit[0].request_body)
