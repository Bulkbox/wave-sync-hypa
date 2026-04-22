"""Unit tests for api.webhook.receive."""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from wave_sync_hypa.wave_sync_hypa.api.webhook import receive


KEY = "phase2-test-secret"


class TestReceive(FrappeTestCase):
	"""The endpoint must auth with x-api-key, log, enqueue, and return 200 — never run a handler inline."""

	def setUp(self):
		"""Enable the integration with a known API key and capture the baseline for restore."""
		self._baseline = {
			"enabled": frappe.db.get_single_value("Wave Settings", "enabled") or 0,
		}
		self._set_key(KEY)
		self._enable()

	def tearDown(self):
		"""Restore baseline enabled flag and clean up any logs written by tests."""
		frappe.db.set_value(
			"Wave Settings",
			"Wave Settings",
			"enabled",
			self._baseline["enabled"],
			update_modified=False,
		)
		frappe.db.commit()
		frappe.clear_document_cache("Wave Settings", "Wave Settings")
		self._cleanup_logs()

	def _set_key(self, key: str) -> None:
		"""Write the inbound API key via the ORM so Password encryption is applied."""
		settings = frappe.get_single("Wave Settings")
		settings.inbound_api_key = key
		settings.flags.ignore_validate = True
		settings.save(ignore_permissions=True)
		frappe.clear_document_cache("Wave Settings", "Wave Settings")

	def _enable(self) -> None:
		"""Flip the enabled flag on via direct DB write (bypasses validate() for other fields)."""
		frappe.db.set_value(
			"Wave Settings", "Wave Settings", "enabled", 1, update_modified=False
		)
		frappe.db.commit()
		frappe.clear_document_cache("Wave Settings", "Wave Settings")

	def _cleanup_logs(self) -> None:
		"""Delete all Wave Sync Log rows created during this test."""
		for name in frappe.get_all(
			"Wave Sync Log", filters={"wave_id": self._wave_id}, pluck="name"
		):
			frappe.delete_doc("Wave Sync Log", name, ignore_permissions=True, delete_permanently=True)

	def _wave_id_new(self) -> str:
		"""Generate and remember a unique wave_id so cleanup is scoped to this test."""
		self._wave_id = frappe.generate_hash(length=12)
		return self._wave_id

	def _request(self, headers: dict, query: str, body: dict | None):
		"""Install a minimal Werkzeug request onto frappe.local so the endpoint sees headers/args/body."""
		builder = EnvironBuilder(
			method="POST",
			path=f"/api/method/wave_sync_hypa.wave_sync_hypa.api.webhook.receive?{query}",
			headers=headers,
			data=json.dumps(body) if body is not None else None,
			content_type="application/json",
		)
		req = Request(builder.get_environ())
		frappe.local.request = req
		frappe.local.response = frappe._dict(
			{"docs": [], "messages": [], "type": "json", "http_status_code": 200}
		)
		return req

	def test_200_and_enqueue_on_valid_key(self):
		"""Valid key + shape: log Received + Enqueued, return ok=True, call frappe.enqueue once."""
		wave_id = self._wave_id_new()
		self._request(
			{"x-api-key": KEY},
			"doc=CUSTOMER",
			{"action": "UPDATE", "payload": {"_id": wave_id, "updatedAt": "1"}},
		)
		with patch(
			"wave_sync_hypa.wave_sync_hypa.api.webhook.frappe.enqueue"
		) as mocked_enqueue:
			result = receive()
		self.assertTrue(result["ok"])
		self.assertIn("correlation_id", result)
		mocked_enqueue.assert_called_once()
		kwargs = mocked_enqueue.call_args.kwargs
		self.assertEqual(kwargs["doc_type"], "CUSTOMER")
		self.assertEqual(kwargs["action"], "UPDATE")
		self.assertIn("job_name", kwargs)
		self.assertIn(wave_id, kwargs["job_name"])

		steps = frappe.get_all(
			"Wave Sync Log",
			filters={"correlation_id": result["correlation_id"]},
			fields=["step"],
			order_by="creation asc",
			pluck="step",
		)
		self.assertEqual(steps, ["Received", "Enqueued"])

	def test_403_on_invalid_key(self):
		"""Wrong x-api-key raises PermissionError (HTTP 403); no enqueue happens."""
		wave_id = self._wave_id_new()
		self._request(
			{"x-api-key": "wrong"},
			"doc=CUSTOMER",
			{"action": "UPDATE", "payload": {"_id": wave_id, "updatedAt": "1"}},
		)
		with patch("wave_sync_hypa.wave_sync_hypa.api.webhook.frappe.enqueue") as mocked_enqueue:
			with self.assertRaises(frappe.PermissionError):
				receive()
		mocked_enqueue.assert_not_called()
		self.assertEqual(frappe.local.response.http_status_code, 403)

	def test_403_on_missing_key(self):
		"""No x-api-key at all also raises PermissionError."""
		self._wave_id_new()
		self._request({}, "doc=CUSTOMER", {"action": "UPDATE", "payload": {"_id": "x", "updatedAt": "1"}})
		with self.assertRaises(frappe.PermissionError):
			receive()
		self.assertEqual(frappe.local.response.http_status_code, 403)

	def test_400_on_missing_doc_query(self):
		"""Without ?doc=X the request is malformed and must be rejected with 400."""
		wave_id = self._wave_id_new()
		self._request(
			{"x-api-key": KEY},
			"",
			{"action": "UPDATE", "payload": {"_id": wave_id, "updatedAt": "1"}},
		)
		with self.assertRaises(frappe.ValidationError):
			receive()
		self.assertEqual(frappe.local.response.http_status_code, 400)

	def test_400_on_missing_action(self):
		"""Without body.action we can't route, so reject 400."""
		wave_id = self._wave_id_new()
		self._request({"x-api-key": KEY}, "doc=CUSTOMER", {"payload": {"_id": wave_id, "updatedAt": "1"}})
		with self.assertRaises(frappe.ValidationError):
			receive()
		self.assertEqual(frappe.local.response.http_status_code, 400)

	def test_auth_failure_log_survives_rollback(self):
		"""A rejected webhook must leave an audit row even after Frappe rolls the request back.

		The receive() handler commits the Authenticated/Error row (and the subsequent
		Failed row via _abort) before raising, so the rollback that Frappe's request
		handler performs on an unhandled exception does not erase the audit trail.
		"""
		self._wave_id_new()
		self._request(
			{"x-api-key": "definitely-wrong-key"},
			"doc=CUSTOMER",
			{"action": "UPDATE", "payload": {"_id": "x", "updatedAt": "1"}},
		)
		try:
			receive()
		except frappe.PermissionError:
			pass
		correlation_id = frappe.local.response.get("correlation_id")
		self.assertTrue(correlation_id, "_abort must return a correlation id in the response.")
		# Simulate the rollback Frappe would perform on the exception it just re-raised.
		frappe.db.rollback()
		logs = frappe.get_all(
			"Wave Sync Log",
			filters={"correlation_id": correlation_id},
			fields=["step", "level", "error_message"],
		)
		steps = {row.step for row in logs}
		self.assertIn(
			"Authenticated",
			steps,
			"The Authenticated/Error row must be committed before the 403 raise.",
		)
		self.assertIn(
			"Failed",
			steps,
			"The Failed row in _abort must be committed before the 403 raise.",
		)
		# Cleanup: the committed log rows won't roll back with tearDown's rollback.
		for name in frappe.get_all(
			"Wave Sync Log", filters={"correlation_id": correlation_id}, pluck="name"
		):
			frappe.delete_doc("Wave Sync Log", name, ignore_permissions=True, delete_permanently=True)
		frappe.db.commit()
