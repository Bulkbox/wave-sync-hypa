"""Unit tests for services.payment_review_flag (issue #129).

flag/clear set the review fields and raise/close an accounting ToDo, are
idempotent, and never raise. All frappe.db / frappe.get_doc / frappe.get_all
calls are patched.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.services import payment_review_flag

DT = "Sales Order"
NAME = "SAL-ORD-2026-00105"
ASSIGNEE = "accountant@example.com"


def _settings(assignee=ASSIGNEE):
	s = MagicMock(name="WaveSettings")
	s.get.side_effect = lambda key, default=None: {"wave_payment_review_assignee": assignee}.get(key, default)
	return s


class TestFlag(FrappeTestCase):
	def test_sets_fields_and_creates_todo(self):
		def _exists(doctype, filters=None):
			if doctype == "User":
				return True
			return False  # no open ToDo yet

		with (
			patch.object(frappe.db, "set_value") as mock_set,
			patch.object(frappe.db, "exists", side_effect=_exists),
			patch.object(frappe.db, "get_value", return_value=1),  # User enabled
			patch.object(frappe, "get_doc") as mock_get_doc,
			patch.object(payment_review_flag, "log_step"),
		):
			payment_review_flag.flag(DT, NAME, "iPay has no completed payment yet.", settings=_settings())

		fields = mock_set.call_args.args[2]
		self.assertEqual(fields["wave_payment_review_required"], 1)
		self.assertIn("iPay has no completed payment", fields["wave_payment_review_reason"])
		mock_get_doc.assert_called_once()  # ToDo created
		self.assertEqual(mock_get_doc.call_args.args[0]["doctype"], "ToDo")

	def test_no_assignee_skips_todo_but_still_flags(self):
		with (
			patch.object(frappe.db, "set_value") as mock_set,
			patch.object(frappe, "get_doc") as mock_get_doc,
			patch.object(payment_review_flag, "log_step"),
		):
			payment_review_flag.flag(DT, NAME, "reason", settings=_settings(assignee=""))
		mock_set.assert_called_once()
		mock_get_doc.assert_not_called()

	def test_dedup_skips_when_open_todo_exists(self):
		def _exists(doctype, filters=None):
			return True  # User exists AND an open ToDo already exists

		with (
			patch.object(frappe.db, "set_value"),
			patch.object(frappe.db, "exists", side_effect=_exists),
			patch.object(frappe.db, "get_value", return_value=1),
			patch.object(frappe, "get_doc") as mock_get_doc,
			patch.object(payment_review_flag, "log_step"),
		):
			payment_review_flag.flag(DT, NAME, "reason", settings=_settings())
		mock_get_doc.assert_not_called()

	def test_never_raises(self):
		with (
			patch.object(frappe.db, "set_value", side_effect=RuntimeError("db down")),
			patch.object(payment_review_flag, "log_step") as mock_log,
		):
			payment_review_flag.flag(DT, NAME, "reason", settings=_settings())  # no raise
		levels = [c.kwargs.get("level") for c in mock_log.call_args_list]
		self.assertIn("Error", levels)


class TestClear(FrappeTestCase):
	def test_clears_fields_and_closes_todos(self):
		with (
			patch.object(frappe.db, "set_value") as mock_set,
			patch.object(frappe, "get_all", return_value=["TODO-0001"]),
			patch.object(payment_review_flag, "log_step"),
		):
			payment_review_flag.clear(DT, NAME, settings=_settings())

		# First set_value clears the doc fields; a later one closes the ToDo.
		first_fields = mock_set.call_args_list[0].args[2]
		self.assertEqual(first_fields["wave_payment_review_required"], 0)
		self.assertIsNone(first_fields["wave_payment_review_reason"])
		closed = [c for c in mock_set.call_args_list if c.args[0] == "ToDo"]
		self.assertEqual(len(closed), 1)
		self.assertEqual(closed[0].args[3], "Closed")
