"""Unit tests for services.intake_review_notifier."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.services import intake_review_notifier as irn


def _settings(*, enabled=1, assignee="", role="") -> MagicMock:
	"""Wave Settings stand-in carrying the three ToDo fields."""
	doc = MagicMock(name="WaveSettings")
	doc.get.side_effect = lambda key, default=None: {
		"wave_intake_review_todo_enabled": enabled,
		"wave_intake_review_assignee": assignee,
		"wave_intake_review_role": role,
	}.get(key, default)
	return doc


def _so(name="SAL-ORD-2026-99999") -> SimpleNamespace:
	"""Tiny SO doc stand-in carrying just `name`."""
	return SimpleNamespace(name=name)


class TestTodoMasterSwitch(FrappeTestCase):
	def test_disabled_creates_nothing_for_sales_order(self):
		with patch.object(frappe, "get_doc") as mock_get_doc:
			created = irn.notify_sales_order_needs_review(_so(), _settings(enabled=0), "1 item")
		self.assertEqual(created, 0)
		mock_get_doc.assert_not_called()

	def test_disabled_creates_nothing_for_aborted(self):
		with patch.object(frappe, "get_doc") as mock_get_doc:
			created = irn.notify_intake_aborted(_settings(enabled=0), {"friendlyId": "X"}, "no items")
		self.assertEqual(created, 0)
		mock_get_doc.assert_not_called()


class TestRecipientResolution(FrappeTestCase):
	"""User wins over Role; inactive users filtered; missing recipient short-circuits."""

	def test_assignee_user_wins_over_role(self):
		settings = _settings(assignee="ops@example.com", role="Item Master")
		with patch.object(frappe.db, "exists", return_value=True), \
		     patch.object(frappe.db, "get_value", return_value=1):
			recipients = irn._resolve_recipients(settings)
		self.assertEqual(recipients, ["ops@example.com"])

	def test_inactive_assignee_returns_empty(self):
		"""Disabled User -> no recipient (we don't silently promote to Role)."""
		settings = _settings(assignee="ops@example.com", role="Item Master")
		with patch.object(frappe.db, "exists", return_value=True), \
		     patch.object(frappe.db, "get_value", return_value=0):
			recipients = irn._resolve_recipients(settings)
		self.assertEqual(recipients, [])

	def test_non_existent_assignee_returns_empty(self):
		settings = _settings(assignee="missing@example.com")
		with patch.object(frappe.db, "exists", return_value=False):
			recipients = irn._resolve_recipients(settings)
		self.assertEqual(recipients, [])

	def test_role_fan_out_when_no_assignee(self):
		settings = _settings(role="Item Master")
		with (
			patch.object(frappe, "get_all", return_value=[
				{"parent": "alice@example.com"},
				{"parent": "bob@example.com"},
			]),
			patch.object(frappe.db, "get_value", return_value=1),  # both enabled
		):
			recipients = irn._resolve_recipients(settings)
		self.assertEqual(recipients, ["alice@example.com", "bob@example.com"])

	def test_role_filters_inactive_users(self):
		settings = _settings(role="Item Master")
		with (
			patch.object(frappe, "get_all", return_value=[
				{"parent": "alice@example.com"},
				{"parent": "bob@example.com"},
			]),
			# alice enabled, bob disabled
			patch.object(
				frappe.db, "get_value",
				side_effect=lambda dt, name, field: 1 if name == "alice@example.com" else 0,
			),
		):
			recipients = irn._resolve_recipients(settings)
		self.assertEqual(recipients, ["alice@example.com"])

	def test_no_assignee_no_role_returns_empty(self):
		"""Half-configured site: master switch on but neither User nor Role set -> no-op."""
		recipients = irn._resolve_recipients(_settings(assignee="", role=""))
		self.assertEqual(recipients, [])


class TestNotifySalesOrderNeedsReview(FrappeTestCase):
	def test_creates_one_todo_per_recipient_linked_to_so_medium_priority(self):
		settings = _settings(assignee="ops@example.com")
		with (
			patch.object(frappe.db, "exists", return_value=True),
			patch.object(frappe.db, "get_value", return_value=1),
			patch.object(frappe, "get_doc", return_value=MagicMock()) as mock_get_doc,
		):
			created = irn.notify_sales_order_needs_review(
				_so("SO-1"), settings, "1 unresolved item(s)"
			)
		self.assertEqual(created, 1)
		todo_dict = mock_get_doc.call_args.args[0]
		self.assertEqual(todo_dict["doctype"], "ToDo")
		self.assertEqual(todo_dict["allocated_to"], "ops@example.com")
		self.assertEqual(todo_dict["priority"], "Medium")
		self.assertEqual(todo_dict["reference_type"], "Sales Order")
		self.assertEqual(todo_dict["reference_name"], "SO-1")
		self.assertIn("SO-1", todo_dict["description"])
		self.assertIn("unresolved item", todo_dict["description"])

	def test_returns_zero_when_no_recipients_configured(self):
		with patch.object(frappe, "get_doc") as mock_get_doc:
			created = irn.notify_sales_order_needs_review(
				_so(), _settings(assignee="", role=""), "1 item"
			)
		self.assertEqual(created, 0)
		mock_get_doc.assert_not_called()


class TestNotifyIntakeAborted(FrappeTestCase):
	def test_creates_standalone_high_priority_todo(self):
		settings = _settings(assignee="ops@example.com")
		with (
			patch.object(frappe.db, "exists", return_value=True),
			patch.object(frappe.db, "get_value", return_value=1),
			patch.object(frappe, "get_doc", return_value=MagicMock()) as mock_get_doc,
		):
			created = irn.notify_intake_aborted(
				settings, {"friendlyId": "10000070", "_id": "w-x"}, "no items"
			)
		self.assertEqual(created, 1)
		todo_dict = mock_get_doc.call_args.args[0]
		self.assertEqual(todo_dict["priority"], "High")
		# Standalone ToDo: no SO reference.
		self.assertIsNone(todo_dict["reference_type"])
		self.assertIsNone(todo_dict["reference_name"])
		# Friendly id surfaces in the body so the recipient knows what to look up.
		self.assertIn("10000070", todo_dict["description"])
