"""Unit tests for the email-based secondary lookup in customer_resolver.

Pure: every test patches frappe.db calls so we exercise the branching logic
without touching real Customer / Contact / Dynamic Link rows. The integration
shape (full upsert through handle()) is covered by test_customer_handler.py;
here we focus on:

  * find_customer_by_email — 8 branches (empty / not found / single safe /
    single conflicting / multiple / case-insensitivity / whitespace / 1-with-
    matching-wave-id)
  * find_or_create_customer — the three-step waterfall and source labelling
  * _stamp_wave_customer_id — adoption side-effect calls db.set_value
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.resolvers import customer_resolver as cr


def _payload(**overrides) -> dict:
	"""Minimal CUSTOMER.UPDATE payload."""
	base = {
		"_id": "wave-NEW-id",
		"email": "rashid@hypa.example",
		"firstName": "Rashid",
		"lastName": "Salim",
		"mobilePhone": "+254700000000",
		"isGuest": False,
	}
	base.update(overrides)
	return base


class TestFindCustomerByEmail(FrappeTestCase):
	"""All branches of the email cross-join lookup."""

	def test_empty_email_returns_none_without_querying(self):
		with patch.object(frappe.db, "sql") as mock_sql:
			self.assertIsNone(cr.find_customer_by_email(None, "wave-x"))
			self.assertIsNone(cr.find_customer_by_email("", "wave-x"))
			self.assertIsNone(cr.find_customer_by_email("   ", "wave-x"))
		mock_sql.assert_not_called()

	def test_no_match_returns_none(self):
		with patch.object(frappe.db, "sql", return_value=[]):
			self.assertIsNone(cr.find_customer_by_email("rashid@hypa.example", "wave-x"))

	def test_single_match_with_no_existing_wave_id_returns_that_customer(self):
		"""The classic adoption case: ERP customer was manually created or imported."""
		with (
			patch.object(
				frappe.db, "sql", return_value=[{"customer_name": "CUST-001"}],
			),
			patch.object(frappe.db, "get_value", return_value=""),  # blank existing wave_id
		):
			self.assertEqual(
				cr.find_customer_by_email("rashid@hypa.example", "wave-NEW-id"),
				"CUST-001",
			)

	def test_single_match_with_matching_wave_id_returns_that_customer(self):
		"""Edge case: primary lookup somehow missed but the customer is actually wave-linked.
		The function returns the same name (caller will adopt as a no-op stamp)."""
		with (
			patch.object(
				frappe.db, "sql", return_value=[{"customer_name": "CUST-001"}],
			),
			patch.object(frappe.db, "get_value", return_value="wave-NEW-id"),
		):
			self.assertEqual(
				cr.find_customer_by_email("rashid@hypa.example", "wave-NEW-id"),
				"CUST-001",
			)

	def test_single_match_with_conflicting_wave_id_logs_and_returns_none(self):
		"""Two distinct Wave accounts share an email — never silently merge."""
		with (
			patch.object(
				frappe.db, "sql", return_value=[{"customer_name": "CUST-001"}],
			),
			patch.object(frappe.db, "get_value", return_value="wave-OTHER-id"),
			patch.object(frappe, "log_error") as mock_log,
		):
			self.assertIsNone(
				cr.find_customer_by_email("rashid@hypa.example", "wave-NEW-id"),
			)
		self.assertEqual(mock_log.call_count, 1)
		# The error message names both wave ids so the operator can investigate.
		msg = mock_log.call_args.kwargs.get("message", "")
		self.assertIn("wave-NEW-id", msg)
		self.assertIn("CUST-001", msg)

	def test_multiple_matches_logs_and_returns_none(self):
		"""Two existing ERP customers share an email; we can't safely pick one."""
		with (
			patch.object(
				frappe.db,
				"sql",
				return_value=[
					{"customer_name": "CUST-001"},
					{"customer_name": "CUST-002"},
				],
			),
			patch.object(frappe.db, "get_value", return_value=""),
			patch.object(frappe, "log_error") as mock_log,
		):
			self.assertIsNone(
				cr.find_customer_by_email("rashid@hypa.example", "wave-NEW-id"),
			)
		self.assertEqual(mock_log.call_count, 1)
		msg = mock_log.call_args.kwargs.get("message", "")
		self.assertIn("CUST-001", msg)
		self.assertIn("CUST-002", msg)

	def test_sql_query_uses_lower_for_case_insensitive_match(self):
		"""LOWER() must appear in the SQL so case differences don't miss matches."""
		with patch.object(
			frappe.db, "sql", return_value=[{"customer_name": "CUST-001"}],
		) as mock_sql, patch.object(frappe.db, "get_value", return_value=""):
			cr.find_customer_by_email("RASHID@HYPA.EXAMPLE", "wave-x")
		query = mock_sql.call_args.args[0]
		self.assertIn("LOWER", query)
		# Email argument passed in is the original case; LOWER() handles both sides.
		self.assertEqual(mock_sql.call_args.args[1], ("RASHID@HYPA.EXAMPLE",))

	def test_one_safe_candidate_among_two_matches_still_returns_none(self):
		"""When 2 customers match by email and 1 has a conflicting wave_id, we still
		decline to adopt the remaining one — too risky."""
		with (
			patch.object(
				frappe.db,
				"sql",
				return_value=[
					{"customer_name": "CUST-001"},
					{"customer_name": "CUST-002"},
				],
			),
			patch.object(
				frappe.db,
				"get_value",
				side_effect=lambda dt, name, field: {
					"CUST-001": "wave-OTHER-id",
					"CUST-002": "",
				}[name],
			),
			patch.object(frappe, "log_error") as mock_log,
		):
			self.assertIsNone(
				cr.find_customer_by_email("rashid@hypa.example", "wave-NEW-id"),
			)
		# Only one safe candidate but the situation is still ambiguous — log + skip.
		self.assertEqual(mock_log.call_count, 1)


class TestStampWaveCustomerId(FrappeTestCase):
	def test_writes_via_set_value_without_modified_bump(self):
		with patch.object(frappe.db, "set_value") as mock_set:
			cr._stamp_wave_customer_id("CUST-001", "wave-NEW-id")
		mock_set.assert_called_once_with(
			"Customer", "CUST-001", "wave_customer_id", "wave-NEW-id",
			update_modified=False,
		)

	def test_blank_wave_id_is_a_noop(self):
		with patch.object(frappe.db, "set_value") as mock_set:
			cr._stamp_wave_customer_id("CUST-001", None)
			cr._stamp_wave_customer_id("CUST-001", "")
		mock_set.assert_not_called()


class TestFindOrCreateCustomerWaterfall(FrappeTestCase):
	"""The (name, created, source) three-step waterfall."""

	def test_guest_returns_walk_in_with_source_guest(self):
		payload = _payload(isGuest=True)
		with patch.object(
			frappe.db, "get_single_value", return_value="Walk-In Customer",
		):
			name, created, source = cr.find_or_create_customer(payload)
		self.assertEqual(name, "Walk-In Customer")
		self.assertFalse(created)
		self.assertEqual(source, "guest")

	def test_primary_wave_id_match_short_circuits_with_source_wave_id(self):
		"""Email lookup should not even run when the primary hits, regardless of setting."""
		with (
			patch.object(cr, "find_customer_by_wave_id", return_value="CUST-001"),
			patch.object(cr, "_email_fallback_enabled", return_value=True),
			patch.object(cr, "find_customer_by_email") as mock_email,
			patch.object(cr, "_create_customer_from_wave") as mock_create,
		):
			name, created, source = cr.find_or_create_customer(_payload())
		self.assertEqual(name, "CUST-001")
		self.assertFalse(created)
		self.assertEqual(source, "wave_id")
		mock_email.assert_not_called()
		mock_create.assert_not_called()

	def test_email_match_adopts_and_stamps_wave_id(self):
		"""When the setting is on and email matches, adopt + stamp."""
		with (
			patch.object(cr, "find_customer_by_wave_id", return_value=None),
			patch.object(cr, "_email_fallback_enabled", return_value=True),
			patch.object(cr, "find_customer_by_email", return_value="CUST-001"),
			patch.object(cr, "_stamp_wave_customer_id") as mock_stamp,
			patch.object(cr, "_create_customer_from_wave") as mock_create,
		):
			name, created, source = cr.find_or_create_customer(_payload())
		self.assertEqual(name, "CUST-001")
		self.assertFalse(created)
		self.assertEqual(source, "email")
		mock_stamp.assert_called_once_with("CUST-001", "wave-NEW-id")
		mock_create.assert_not_called()

	def test_email_fallback_off_skips_email_lookup_and_creates_new(self):
		"""When the setting is off, the email step is bypassed entirely; new record created."""
		with (
			patch.object(cr, "find_customer_by_wave_id", return_value=None),
			patch.object(cr, "_email_fallback_enabled", return_value=False),
			patch.object(cr, "find_customer_by_email") as mock_email,
			patch.object(cr, "_create_customer_from_wave", return_value="CUST-NEW"),
		):
			name, created, source = cr.find_or_create_customer(_payload())
		self.assertEqual(name, "CUST-NEW")
		self.assertTrue(created)
		self.assertEqual(source, "new")
		# Setting off => no email lookup at all, even if a match would have existed.
		mock_email.assert_not_called()

	def test_no_match_creates_new_with_source_new(self):
		"""Setting on but no email match -> create new, source=new."""
		with (
			patch.object(cr, "find_customer_by_wave_id", return_value=None),
			patch.object(cr, "_email_fallback_enabled", return_value=True),
			patch.object(cr, "find_customer_by_email", return_value=None),
			patch.object(cr, "_create_customer_from_wave", return_value="CUST-NEW"),
		):
			name, created, source = cr.find_or_create_customer(_payload())
		self.assertEqual(name, "CUST-NEW")
		self.assertTrue(created)
		self.assertEqual(source, "new")


class TestEmailFallbackEnabledHelper(FrappeTestCase):
	"""The _email_fallback_enabled helper reads the Wave Settings flag."""

	def test_returns_true_when_settings_field_is_truthy(self):
		with patch.object(frappe.db, "get_single_value", return_value=1):
			self.assertTrue(cr._email_fallback_enabled())

	def test_returns_false_when_settings_field_is_zero_or_blank(self):
		for v in (0, None, ""):
			with patch.object(frappe.db, "get_single_value", return_value=v):
				self.assertFalse(cr._email_fallback_enabled(), f"value={v!r}")
