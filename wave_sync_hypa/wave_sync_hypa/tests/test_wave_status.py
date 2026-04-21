"""Unit tests for the Wave Status DocType and its integration with rule tables."""

import frappe
from frappe.tests.utils import FrappeTestCase


EXPECTED_SEEDS = {
	"PENDING",
	"ACCEPTED",
	"REJECTED",
	"UNDER_PICKING",
	"UNDER_DELIVERY",
	"PAYMENT_PENDING",
	"COMPLETED",
	"CANCELLED",
}


class TestWaveStatusCatalogue(FrappeTestCase):
	"""The DocType must exist, the seeds must be present, and the rule fields must Link to it."""

	def test_doctype_exists(self):
		"""Wave Status is installed as a standard DocType owned by the app's module."""
		self.assertTrue(frappe.db.exists("DocType", "Wave Status"))
		module = frappe.db.get_value("DocType", "Wave Status", "module")
		self.assertEqual(module, "Wave Sync Hypa")

	def test_all_default_seeds_are_present(self):
		"""The eight default statuses ship via fixtures and show up after migrate."""
		existing = set(frappe.get_all("Wave Status", pluck="name"))
		missing = EXPECTED_SEEDS - existing
		self.assertFalse(missing, f"Missing default Wave Status rows: {missing}")

	def test_inbound_rule_field_links_to_wave_status(self):
		"""Wave Status Rule Inbound.wave_status must be a Link with options=Wave Status."""
		field = frappe.get_meta("Wave Status Rule Inbound").get_field("wave_status")
		self.assertEqual(field.fieldtype, "Link")
		self.assertEqual(field.options, "Wave Status")

	def test_outbound_rule_field_links_to_wave_status(self):
		"""Wave Status Rule Outbound.wave_status must be a Link with options=Wave Status."""
		field = frappe.get_meta("Wave Status Rule Outbound").get_field("wave_status")
		self.assertEqual(field.fieldtype, "Link")
		self.assertEqual(field.options, "Wave Status")


class TestCustomWaveStatus(FrappeTestCase):
	"""An operator can add a new Wave Status and reference it from a rule row."""

	def setUp(self):
		"""Create a one-off status for this test; remove it in tearDown."""
		self.new_status = "PHASE3_TEST_STATUS"
		if not frappe.db.exists("Wave Status", self.new_status):
			doc = frappe.get_doc(
				{
					"doctype": "Wave Status",
					"status_name": self.new_status,
					"direction": "Both",
					"enabled": 1,
					"description": "Test-only status; safe to delete.",
				}
			)
			doc.insert(ignore_permissions=True)

	def tearDown(self):
		"""Delete the custom status and any rule row referencing it."""
		frappe.db.delete(
			"Wave Status Rule Inbound", {"wave_status": self.new_status}
		)
		frappe.db.commit()
		frappe.clear_document_cache("Wave Settings", "Wave Settings")
		if frappe.db.exists("Wave Status", self.new_status):
			frappe.delete_doc(
				"Wave Status", self.new_status, ignore_permissions=True, delete_permanently=True
			)

	def test_rule_row_can_reference_custom_status(self):
		"""An Inbound rule row saved with the new Link value persists without error."""
		settings = frappe.get_single("Wave Settings")
		settings.append(
			"inbound_status_rules",
			{
				"wave_status": self.new_status,
				"erp_action_key": "ignore",
				"enabled": 1,
				"notes": "added by test",
			},
		)
		settings.flags.ignore_validate = True
		settings.save(ignore_permissions=True)
		frappe.clear_document_cache("Wave Settings", "Wave Settings")

		reloaded = frappe.get_single("Wave Settings")
		saved_statuses = {row.wave_status for row in reloaded.inbound_status_rules}
		self.assertIn(self.new_status, saved_statuses)
