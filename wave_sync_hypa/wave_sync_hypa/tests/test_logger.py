"""Unit tests for services.logger.log_step."""

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.services.logger import log_step


class TestLogStep(FrappeTestCase):
	"""Each test writes at most one Wave Sync Log row and cleans it up in tearDown."""

	def setUp(self):
		"""Capture a fresh correlation id for the row under test."""
		self.correlation_id = frappe.generate_hash(length=16)

	def tearDown(self):
		"""Delete any log rows this test created."""
		names = frappe.get_all(
			"Wave Sync Log",
			filters={"correlation_id": self.correlation_id},
			pluck="name",
		)
		for name in names:
			frappe.delete_doc("Wave Sync Log", name, ignore_permissions=True, delete_permanently=True)

	def test_writes_a_single_row_with_required_fields(self):
		"""A basic call persists one row with the correlation_id, step, and level."""
		log_step(self.correlation_id, "Received", "Info", doc_type="CUSTOMER", action="UPDATE")
		rows = frappe.get_all(
			"Wave Sync Log",
			filters={"correlation_id": self.correlation_id},
			fields=["step", "level", "doc_type", "action"],
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].step, "Received")
		self.assertEqual(rows[0].level, "Info")
		self.assertEqual(rows[0].doc_type, "CUSTOMER")
		self.assertEqual(rows[0].action, "UPDATE")

	def test_serialises_request_body_as_json_string(self):
		"""Dict payloads are stored as JSON text, readable by humans in the Desk."""
		payload = {"action": "UPDATE", "payload": {"_id": "abc", "email": "x@y"}}
		log_step(self.correlation_id, "Processing", "Info", request_body=payload)
		row = frappe.get_value(
			"Wave Sync Log",
			{"correlation_id": self.correlation_id},
			"request_body",
		)
		self.assertIn("\"_id\": \"abc\"", row)
		self.assertIn("\"email\": \"x@y\"", row)

	def test_error_level_fields_are_persisted(self):
		"""Error rows carry the message and trace for triage."""
		log_step(
			self.correlation_id,
			"Failed",
			"Error",
			error_message="Customer not found",
			stack_trace="Traceback: ...",
		)
		row = frappe.get_value(
			"Wave Sync Log",
			{"correlation_id": self.correlation_id},
			["error_message", "stack_trace"],
			as_dict=True,
		)
		self.assertEqual(row.error_message, "Customer not found")
		self.assertEqual(row.stack_trace, "Traceback: ...")

	def test_pipeline_specific_step_tag_persists(self):
		"""Free-form snake_case step tags (used by stock-sync, order-status, etc.) must round-trip.

		Regression cover: when this field was a Select limited to 13 canonical
		options, every stock_sync_* / order_status_push_* row was rejected by
		validation and silently swallowed by log_step's try/except — telemetry
		loss for ~a month before anyone noticed. Asserting persistence here
		means a future tightening of the field type would fail this test
		instead of the production pipeline.
		"""
		log_step(self.correlation_id, "stock_sync_push_attempt", "Info", linked_doctype="Item")
		step = frappe.db.get_value(
			"Wave Sync Log",
			{"correlation_id": self.correlation_id},
			"step",
		)
		self.assertEqual(step, "stock_sync_push_attempt")
