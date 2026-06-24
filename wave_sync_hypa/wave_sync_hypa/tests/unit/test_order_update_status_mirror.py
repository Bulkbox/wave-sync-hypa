"""Unit tests for order_update._mirror_wave_status_to_so (inbound Wave status mirror).

Pure-function: reads payload _id/status, mirrors onto the linked Sales Order's
wave_status, logs only when the value actually changes.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers import order_update

WAVE_ID = "wave-upd-1"


class TestMirrorWaveStatusToSo(FrappeTestCase):
	def test_changed_status_is_written_and_logged(self):
		so = SimpleNamespace(name="SO-1", wave_status="ACCEPTED")
		with (
			patch.object(frappe.db, "get_value", return_value=so),
			patch.object(frappe.db, "set_value") as mock_set,
			patch.object(order_update, "log_step") as mock_log,
		):
			order_update._mirror_wave_status_to_so({"_id": WAVE_ID, "status": "UNDER_DELIVERY"}, "corr")
		mock_set.assert_called_once_with("Sales Order", "SO-1", "wave_status", "UNDER_DELIVERY", update_modified=False)
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(order_update.STEP_STATUS_MIRRORED, steps)

	def test_unchanged_status_is_noop(self):
		so = SimpleNamespace(name="SO-1", wave_status="UNDER_DELIVERY")
		with (
			patch.object(frappe.db, "get_value", return_value=so),
			patch.object(frappe.db, "set_value") as mock_set,
			patch.object(order_update, "log_step") as mock_log,
		):
			order_update._mirror_wave_status_to_so({"_id": WAVE_ID, "status": "UNDER_DELIVERY"}, "corr")
		mock_set.assert_not_called()
		mock_log.assert_not_called()

	def test_missing_status_or_wave_id_is_noop(self):
		with (
			patch.object(frappe.db, "get_value") as mock_get,
			patch.object(frappe.db, "set_value") as mock_set,
		):
			order_update._mirror_wave_status_to_so({"_id": "", "status": "X"}, "corr")
			order_update._mirror_wave_status_to_so({"_id": WAVE_ID, "status": ""}, "corr")
		mock_get.assert_not_called()
		mock_set.assert_not_called()

	def test_no_matching_sales_order_is_noop(self):
		with (
			patch.object(frappe.db, "get_value", return_value=None),
			patch.object(frappe.db, "set_value") as mock_set,
			patch.object(order_update, "log_step") as mock_log,
		):
			order_update._mirror_wave_status_to_so({"_id": WAVE_ID, "status": "COMPLETED"}, "corr")
		mock_set.assert_not_called()
		mock_log.assert_not_called()
