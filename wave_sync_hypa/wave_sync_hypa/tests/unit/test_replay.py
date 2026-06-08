"""Unit tests for operator replay: processor force flag + api.replay.replay_order (issue #144)."""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.api import replay
from wave_sync_hypa.wave_sync_hypa.services import processor

CORR = "corr-1"
PAYLOAD = {"_id": "W1", "updatedAt": "123", "friendlyId": "100"}
ENVELOPE = '{"action": "CREATE", "payload": {"_id": "W1", "updatedAt": "123", "friendlyId": "100"}}'


class TestProcessorForce(FrappeTestCase):
	"""force=True bypasses the updated_at duplicate check; the default keeps it."""

	def _run(self, force):
		with (
			patch.object(processor, "is_wave_integration_enabled", return_value=True),
			patch.object(processor, "is_duplicate", return_value=True),
			patch.object(processor, "resolve_handler", return_value=MagicMock()),
			patch.object(processor, "_run_handler") as mock_run,
			patch.object(processor, "log_step"),
		):
			processor.process_webhook(CORR, "ORDER", "CREATE", PAYLOAD, force=force)
		return mock_run

	def test_force_bypasses_duplicate_check(self):
		self.assertTrue(self._run(force=True).called)

	def test_default_honours_duplicate_check(self):
		self.assertFalse(self._run(force=False).called)


class TestReplayOrder(FrappeTestCase):
	"""Reconstruct the stored payload and re-process it with force=True."""

	def test_replays_with_force_and_new_correlation(self):
		row = frappe._dict(doc_type="ORDER", action="CREATE", request_body=ENVELOPE)
		with (
			patch.object(frappe, "has_permission", return_value=True),
			patch.object(frappe.db, "get_value", return_value=row),
			patch.object(replay, "new_correlation_id", return_value="corr-new"),
			patch.object(replay, "process_webhook") as mock_proc,
		):
			result = replay.replay_order(CORR)
		self.assertEqual(result, {"ok": True, "correlation_id": "corr-new"})
		args, kwargs = mock_proc.call_args
		self.assertEqual(args[:3], ("corr-new", "ORDER", "CREATE"))
		self.assertEqual(args[3]["_id"], "W1")
		self.assertTrue(kwargs.get("force"))

	def test_permission_denied_raises(self):
		with patch.object(frappe, "has_permission", return_value=False):
			with self.assertRaises(frappe.PermissionError):
				replay.replay_order(CORR)

	def test_missing_received_row_raises(self):
		with (
			patch.object(frappe, "has_permission", return_value=True),
			patch.object(frappe.db, "get_value", return_value=None),
		):
			with self.assertRaises(frappe.ValidationError):
				replay.replay_order(CORR)

	def test_envelope_without_payload_raises(self):
		row = frappe._dict(doc_type="ORDER", action="CREATE", request_body='{"action": "CREATE"}')
		with (
			patch.object(frappe, "has_permission", return_value=True),
			patch.object(frappe.db, "get_value", return_value=row),
		):
			with self.assertRaises(frappe.ValidationError):
				replay.replay_order(CORR)
