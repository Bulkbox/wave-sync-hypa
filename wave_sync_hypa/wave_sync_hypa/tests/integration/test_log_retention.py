"""Unit tests for tasks.log_retention.purge_old_logs."""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, now_datetime

from wave_sync_hypa.wave_sync_hypa.services.logger import log_step
from wave_sync_hypa.wave_sync_hypa.tasks.log_retention import purge_old_logs


class TestPurgeOldLogs(FrappeTestCase):
	"""Seed rows at known creation dates, then assert only the old ones are deleted."""

	def setUp(self):
		"""Use a unique correlation id per test and write a known retention window directly."""
		self.correlation_id = frappe.generate_hash(length=16)
		self._baseline_retention = frappe.db.get_single_value("Wave Settings", "log_retention_days") or 14
		self._write_retention_days(14)

	def tearDown(self):
		"""Remove anything this test created and restore the baseline retention value."""
		names = frappe.get_all(
			"Wave Sync Log",
			filters={"correlation_id": self.correlation_id},
			pluck="name",
		)
		for name in names:
			frappe.delete_doc("Wave Sync Log", name, ignore_permissions=True, delete_permanently=True)
		self._write_retention_days(self._baseline_retention)

	def _write_retention_days(self, days: int) -> None:
		"""Persist a retention window via direct DB write to bypass unrelated validate() checks."""
		frappe.db.set_value(
			"Wave Settings", "Wave Settings", "log_retention_days", days, update_modified=False
		)
		frappe.db.commit()

	def _backdate(self, log_name: str, creation) -> None:
		"""Set a row's creation to a specific timestamp via direct SQL (bypasses framework defaults)."""
		frappe.db.set_value("Wave Sync Log", log_name, "creation", creation, update_modified=False)
		frappe.db.commit()

	def test_only_rows_older_than_retention_window_are_deleted(self):
		"""Seed one fresh row (age 1 day) and one stale row (age 20 days); only the stale one purges."""
		fresh_name = log_step(self.correlation_id, "Received", "Info")
		stale_name = log_step(self.correlation_id, "Received", "Info")
		self._backdate(fresh_name, add_days(now_datetime(), -1))
		self._backdate(stale_name, add_days(now_datetime(), -20))

		deleted = purge_old_logs()

		self.assertGreaterEqual(deleted, 1)
		self.assertTrue(frappe.db.exists("Wave Sync Log", fresh_name))
		self.assertFalse(frappe.db.exists("Wave Sync Log", stale_name))

	def test_respects_configured_retention_window(self):
		"""A 1-day retention window purges a 2-day-old row that a 14-day window would keep."""
		self._write_retention_days(1)
		name = log_step(self.correlation_id, "Received", "Info")
		self._backdate(name, add_days(now_datetime(), -2))

		deleted = purge_old_logs()

		self.assertGreaterEqual(deleted, 1)
		self.assertFalse(frappe.db.exists("Wave Sync Log", name))
