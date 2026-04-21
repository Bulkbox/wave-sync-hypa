"""Unit tests for services.processor.process_webhook."""

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.services import dispatcher
from wave_sync_hypa.wave_sync_hypa.services.processor import process_webhook


class TestProcessWebhook(FrappeTestCase):
	"""Assert the processor logs the right outcome for each dispatch path."""

	def setUp(self):
		"""Unique correlation id per test, fresh registry stub, empty route_rules."""
		self.correlation_id = frappe.generate_hash(length=16)
		self.wave_id = frappe.generate_hash(length=12)
		self.updated_at = "1776753292987"
		self._clear_rules()
		self._original_handler = dispatcher.HANDLER_REGISTRY.get("customer_upsert")
		self._handler_calls: list[tuple] = []
		dispatcher.HANDLER_REGISTRY["customer_upsert"] = self._record_handler

	def tearDown(self):
		"""Restore registry, wipe logs created under this correlation id, clear rules."""
		dispatcher.HANDLER_REGISTRY["customer_upsert"] = self._original_handler
		for name in frappe.get_all(
			"Wave Sync Log", filters={"correlation_id": self.correlation_id}, pluck="name"
		):
			frappe.delete_doc("Wave Sync Log", name, ignore_permissions=True, delete_permanently=True)
		self._clear_rules()

	def _clear_rules(self) -> None:
		"""Drop every route_rules row via direct DB writes."""
		frappe.db.delete("Wave Route Rule", {"parent": "Wave Settings"})
		frappe.db.commit()
		frappe.clear_document_cache("Wave Settings", "Wave Settings")

	def _enable_rule(self, doc_type: str, action: str, handler_key: str) -> None:
		"""Append one enabled Wave Route Rule and save without validation."""
		settings = frappe.get_single("Wave Settings")
		settings.append(
			"route_rules",
			{"doc_type": doc_type, "action": action, "handler_key": handler_key, "enabled": 1},
		)
		settings.flags.ignore_validate = True
		settings.save(ignore_permissions=True)
		frappe.clear_document_cache("Wave Settings", "Wave Settings")

	def _record_handler(self, payload, correlation_id):
		"""Test-only handler: stash the args so tests can assert they arrived intact."""
		self._handler_calls.append((payload, correlation_id))

	def _raising_handler(self, payload, correlation_id):
		"""Test-only handler that raises to exercise the Failed log path."""
		raise RuntimeError("handler blew up")

	def _last_step(self) -> str | None:
		"""Return the step of the most recent Wave Sync Log row for this correlation id."""
		row = frappe.get_all(
			"Wave Sync Log",
			filters={"correlation_id": self.correlation_id},
			fields=["step", "level"],
			order_by="creation desc",
			limit=1,
		)
		return row[0].step if row else None

	def _payload(self) -> dict:
		"""Build a minimal Wave-shaped payload for tests."""
		return {"_id": self.wave_id, "updatedAt": self.updated_at, "email": "x@y.com"}

	def test_skipped_when_no_route_rule(self):
		"""No enabled rule for (doc, action) -> log Skipped/Warning and do not invoke any handler."""
		process_webhook(self.correlation_id, "CUSTOMER", "UPDATE", self._payload())
		self.assertEqual(self._last_step(), "Skipped")
		self.assertEqual(self._handler_calls, [])

	def test_skipped_when_handler_not_registered(self):
		"""Rule points at a key whose callable is None (phase not landed) -> log Skipped."""
		self._enable_rule("ORDER", "CREATE", "order_create")  # order_create is still None
		process_webhook(self.correlation_id, "ORDER", "CREATE", self._payload())
		self.assertEqual(self._last_step(), "Skipped")

	def test_completed_on_handler_success(self):
		"""Enabled rule + registered handler -> Processing then Completed; handler receives payload."""
		self._enable_rule("CUSTOMER", "UPDATE", "customer_upsert")
		payload = self._payload()
		process_webhook(self.correlation_id, "CUSTOMER", "UPDATE", payload)
		self.assertEqual(self._last_step(), "Completed")
		self.assertEqual(len(self._handler_calls), 1)
		received_payload, received_corr = self._handler_calls[0]
		self.assertEqual(received_payload, payload)
		self.assertEqual(received_corr, self.correlation_id)

	def test_failed_when_handler_raises(self):
		"""Handler exception is caught, logged Failed/Error, never re-raised out of the worker."""
		self._enable_rule("CUSTOMER", "UPDATE", "customer_upsert")
		dispatcher.HANDLER_REGISTRY["customer_upsert"] = self._raising_handler
		process_webhook(self.correlation_id, "CUSTOMER", "UPDATE", self._payload())
		self.assertEqual(self._last_step(), "Failed")

	def test_skipped_when_already_completed(self):
		"""A second run for the same (wave_id, updated_at) logs Skipped without calling the handler."""
		self._enable_rule("CUSTOMER", "UPDATE", "customer_upsert")
		process_webhook(self.correlation_id, "CUSTOMER", "UPDATE", self._payload())
		self.assertEqual(len(self._handler_calls), 1)
		# Second run under a fresh correlation id but same wave_id + updatedAt
		second_correlation = frappe.generate_hash(length=16)
		process_webhook(second_correlation, "CUSTOMER", "UPDATE", self._payload())
		# Handler should still be called only once.
		self.assertEqual(len(self._handler_calls), 1)
		# And the latest row under the second correlation id is Skipped.
		row = frappe.get_all(
			"Wave Sync Log",
			filters={"correlation_id": second_correlation},
			fields=["step"],
			order_by="creation desc",
			limit=1,
		)
		self.assertEqual(row[0].step, "Skipped")
		# Cleanup the second-run rows.
		for name in frappe.get_all(
			"Wave Sync Log", filters={"correlation_id": second_correlation}, pluck="name"
		):
			frappe.delete_doc("Wave Sync Log", name, ignore_permissions=True, delete_permanently=True)
