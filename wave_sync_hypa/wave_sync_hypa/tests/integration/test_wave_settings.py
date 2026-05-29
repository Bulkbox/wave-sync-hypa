"""Unit tests for the Wave Settings controller.

Test isolation note: this suite mutates the live `Wave Settings` Single
on the running site (no separate test site exists today). Without
explicit snapshot/restore, every test run leaves behind whatever the
last alphabetical test set — historically that meant `inbound_api_key`
flipping to `"E"*32`, `route_rules` getting wiped to zero, etc.

setUp now captures EVERY field a test in this file might mutate, plus
`route_rules` rows; tearDown writes them back exactly. The snapshot
covers:
  - scalar fields (enabled, price_scale_divisor, log_retention_days)
  - the encrypted `inbound_api_key` (via get_password)
  - the entire `route_rules` child table (saved as dict snapshots)
"""

import frappe
from frappe.tests.utils import FrappeTestCase


class TestWaveSettings(FrappeTestCase):
	"""Invariants enforced by WaveSettings.validate."""

	def setUp(self):
		"""Snapshot every field this suite mutates so tearDown can restore the live config bit-for-bit."""
		live = frappe.get_single("Wave Settings")
		self._baseline = {
			"enabled": frappe.db.get_single_value("Wave Settings", "enabled") or 0,
			"price_scale_divisor": frappe.db.get_single_value(
				"Wave Settings", "price_scale_divisor"
			) or 100,
			"log_retention_days": frappe.db.get_single_value(
				"Wave Settings", "log_retention_days"
			) or 14,
		}
		self._baseline_inbound_api_key = (
			live.get_password("inbound_api_key", raise_exception=False) or ""
		)
		# Snapshot every route_rule row (only the fields we care about).
		self._baseline_route_rules = [
			{
				"enabled": int(r.enabled or 0),
				"doc_type": r.doc_type,
				"action": r.action,
				"handler_key": r.handler_key,
			}
			for r in (live.get("route_rules") or [])
		]
		self._write_fields(enabled=0, price_scale_divisor=100, log_retention_days=14)
		self.settings = frappe.get_single("Wave Settings")

	def tearDown(self):
		"""Restore EVERY snapshotted value so the next test (and any operator opening the doc) sees the original config."""
		self._write_fields(**self._baseline)
		# Restore inbound_api_key via the encrypted store directly. Bypass
		# our validate() since we're rolling back, not enforcing.
		self._restore_inbound_api_key()
		# Restore route_rules rows by direct child-table SQL: avoids
		# re-running validate / before_save / on_update on a half-formed
		# settings doc.
		self._restore_route_rules()

	def _write_fields(self, **fields) -> None:
		"""Persist Wave Settings fields via direct DB writes to bypass validate() in fixture lifecycle."""
		for name, value in fields.items():
			frappe.db.set_value("Wave Settings", "Wave Settings", name, value, update_modified=False)
		frappe.db.commit()

	def _restore_inbound_api_key(self) -> None:
		"""Write the snapshotted inbound_api_key cleartext back to __Auth via Frappe's encrypted-set primitive."""
		from frappe.utils.password import remove_encrypted_password, set_encrypted_password

		remove_encrypted_password("Wave Settings", "Wave Settings", "inbound_api_key")
		if self._baseline_inbound_api_key:
			set_encrypted_password(
				"Wave Settings",
				"Wave Settings",
				self._baseline_inbound_api_key,
				"inbound_api_key",
			)
		frappe.db.commit()

	def _restore_route_rules(self) -> None:
		"""Replace the route_rules child table with the setUp snapshot via direct SQL inserts."""
		frappe.db.sql("DELETE FROM `tabWave Route Rule` WHERE parent='Wave Settings'")
		for idx, row in enumerate(self._baseline_route_rules):
			child = frappe.get_doc(
				{
					"doctype": "Wave Route Rule",
					"parent": "Wave Settings",
					"parenttype": "Wave Settings",
					"parentfield": "route_rules",
					"idx": idx + 1,
					**row,
				}
			)
			child.flags.ignore_links = True
			child.insert(ignore_permissions=True)
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

	def _route_rule_count(self) -> int:
		"""Count Wave Route Rule rows currently parented under Wave Settings."""
		return frappe.db.count(
			"Wave Route Rule",
			filters={"parent": "Wave Settings", "parenttype": "Wave Settings"},
		)

	def test_save_with_empty_in_memory_child_table_preserves_db_rows(self):
		"""ANY save with empty in-memory child rows must NOT wipe DB rows — always-protect default.

		Regression cover for the route-rules-disappear bug. Three causes
		hit this same code path:
		  (1) bench migrate / install / patch with an unloaded Single
		  (2) test runs that deliberately blank the table for assertions
		  (3) a buggy form post that doesn't include the rows
		All three are now defended by the same guard. To genuinely clear a
		table the caller must opt in via `flags.allow_child_table_clear`.
		"""
		baseline_count = self._route_rule_count()
		# Seed one extra rule via a normal save.
		self.settings.enabled = 1
		self.settings.inbound_api_key = "G" * 32
		self.settings.append("route_rules", {
			"enabled": 1,
			"doc_type": "ORDER",
			"action": "CREATE",
			"handler_key": "order_create",
		})
		self.settings.flags.ignore_links = True
		self.settings.save(ignore_permissions=True)

		# Save protection means the seeded rule + every pre-existing baseline
		# row survive together.
		seeded = self._route_rule_count()
		self.assertGreaterEqual(seeded, baseline_count + 1)

		# Plain UI-style save with empty in-memory rows — must NOT wipe DB.
		reloaded = frappe.get_single("Wave Settings")
		reloaded.route_rules = []
		reloaded.save(ignore_permissions=True)

		surviving = self._route_rule_count()
		self.assertEqual(surviving, seeded)

	def test_explicit_allow_child_table_clear_flag_actually_clears(self):
		"""Setting `flags.allow_child_table_clear=True` opts out of protection — operator-driven clear works.

		The escape hatch exists for SQL admins / scripts that genuinely
		need to wipe a table. Operators using the form to delete individual
		rows aren't affected (each deletion + save still leaves the other
		rows). This test pins the explicit opt-in path.
		"""
		self.settings.enabled = 1
		self.settings.inbound_api_key = "H" * 32
		self.settings.append("route_rules", {
			"enabled": 1,
			"doc_type": "ORDER",
			"action": "UPDATE",
			"handler_key": "order_update",
		})
		self.settings.flags.ignore_links = True
		self.settings.save(ignore_permissions=True)

		reloaded = frappe.get_single("Wave Settings")
		reloaded.route_rules = []
		reloaded.flags.allow_child_table_clear = True
		reloaded.save(ignore_permissions=True)

		self.assertEqual(self._route_rule_count(), 0)

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
