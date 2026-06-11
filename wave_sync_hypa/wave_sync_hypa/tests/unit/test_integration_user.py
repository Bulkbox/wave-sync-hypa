"""Unit tests for run_as_integration_user — audit attribution of inbound writes.

The processor wraps every handler in this so Wave-created records are owned by
the configured Wave Settings.wave_integration_user instead of Guest. It must be
a no-op when unset, switch + restore the session user when set, honour a
fallback, and hold/restore frappe.flags.ignore_permissions.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.services import integration_user as iu


class TestRunAsIntegrationUser(FrappeTestCase):
	def test_noop_when_unconfigured(self):
		original = frappe.session.user
		with patch.object(iu, "get_integration_user", return_value=""):
			with iu.run_as_integration_user():
				self.assertEqual(frappe.session.user, original)
		self.assertEqual(frappe.session.user, original)

	def test_switches_to_configured_user_and_restores(self):
		original = frappe.session.user
		with (
			patch.object(iu, "get_integration_user", return_value="wave-bot@example.com"),
			patch.object(frappe, "set_user") as mock_set,
		):
			with iu.run_as_integration_user():
				pass
		self.assertEqual(mock_set.call_args_list[0].args[0], "wave-bot@example.com")
		self.assertEqual(mock_set.call_args_list[-1].args[0], original)  # restored

	def test_fallback_used_when_unconfigured(self):
		with (
			patch.object(iu, "get_integration_user", return_value=""),
			patch.object(frappe, "set_user") as mock_set,
		):
			with iu.run_as_integration_user(fallback="wave-fallback@example.com"):
				pass
		self.assertEqual(mock_set.call_args_list[0].args[0], "wave-fallback@example.com")

	def test_configured_user_wins_over_fallback(self):
		with (
			patch.object(iu, "get_integration_user", return_value="wave-bot@example.com"),
			patch.object(frappe, "set_user") as mock_set,
		):
			with iu.run_as_integration_user(fallback="wave-fallback@example.com"):
				pass
		self.assertEqual(mock_set.call_args_list[0].args[0], "wave-bot@example.com")

	def test_ignore_permissions_held_and_restored(self):
		frappe.flags.ignore_permissions = False
		with patch.object(iu, "get_integration_user", return_value=""):
			with iu.run_as_integration_user(ignore_permissions=True):
				self.assertTrue(frappe.flags.ignore_permissions)
			self.assertFalse(frappe.flags.ignore_permissions)
