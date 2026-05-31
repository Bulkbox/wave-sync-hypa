"""Unit tests for the Wave Action DocType and its integration with Route Rules."""

import frappe
from frappe.tests.utils import FrappeTestCase


EXPECTED_SEEDS = {"CREATE", "UPDATE", "DELETE"}


class TestWaveActionCatalogue(FrappeTestCase):
	"""The DocType must exist, seeds must be present, and Route Rule.action must Link to it."""

	def test_doctype_exists(self):
		"""Wave Action is installed as a standard DocType owned by the app's module."""
		self.assertTrue(frappe.db.exists("DocType", "Wave Action"))
		module = frappe.db.get_value("DocType", "Wave Action", "module")
		self.assertEqual(module, "Wave Sync Hypa")

	def test_all_default_seeds_are_present(self):
		"""The three default actions ship via fixtures and are present after migrate."""
		existing = set(frappe.get_all("Wave Action", pluck="name"))
		missing = EXPECTED_SEEDS - existing
		self.assertFalse(missing, f"Missing default Wave Action rows: {missing}")

	def test_route_rule_action_field_links_to_wave_action(self):
		"""Wave Route Rule.action must be a Link with options=Wave Action."""
		field = frappe.get_meta("Wave Route Rule").get_field("action")
		self.assertEqual(field.fieldtype, "Link")
		self.assertEqual(field.options, "Wave Action")


class TestCustomWaveAction(FrappeTestCase):
	"""An operator can add a new Wave Action and reference it from a Route Rule row."""

	def setUp(self):
		"""Create a one-off action for this test; remove it in tearDown."""
		self.new_action = "PHASE36_TEST_ACTION"
		if not frappe.db.exists("Wave Action", self.new_action):
			doc = frappe.get_doc(
				{
					"doctype": "Wave Action",
					"action_name": self.new_action,
					"enabled": 1,
					"description": "Test-only action; safe to delete.",
				}
			)
			doc.insert(ignore_permissions=True)

	def tearDown(self):
		"""Delete the custom action and any Route Rule row referencing it."""
		frappe.db.delete("Wave Route Rule", {"action": self.new_action})
		frappe.db.commit()
		frappe.clear_document_cache("Wave Settings", "Wave Settings")
		if frappe.db.exists("Wave Action", self.new_action):
			frappe.delete_doc(
				"Wave Action", self.new_action, ignore_permissions=True, delete_permanently=True
			)

	def test_route_rule_can_reference_custom_action(self):
		"""A Route Rule row saved with the new Link value persists without error."""
		settings = frappe.get_single("Wave Settings")
		settings.append(
			"route_rules",
			{
				"doc_type": "CUSTOMER",
				"action": self.new_action,
				"handler_key": "customer_upsert",
				"enabled": 1,
			},
		)
		settings.flags.ignore_validate = True
		settings.save(ignore_permissions=True)
		frappe.clear_document_cache("Wave Settings", "Wave Settings")

		reloaded = frappe.get_single("Wave Settings")
		saved_actions = {row.action for row in reloaded.route_rules}
		self.assertIn(self.new_action, saved_actions)
