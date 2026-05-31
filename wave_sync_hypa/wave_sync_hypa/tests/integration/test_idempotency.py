"""Unit tests for services.idempotency.is_duplicate."""

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.services.idempotency import is_duplicate
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step


class TestIsDuplicate(FrappeTestCase):
	"""Return True only when a Completed row already exists for (wave_id, updated_at)."""

	def setUp(self):
		"""Scope each test to a unique correlation id so cleanup can find its rows."""
		self.correlation_id = frappe.generate_hash(length=16)
		self.wave_id = frappe.generate_hash(length=12)
		self.updated_at = "1776753292987"

	def tearDown(self):
		"""Remove every Wave Sync Log row the test created."""
		names = frappe.get_all(
			"Wave Sync Log",
			filters={"correlation_id": self.correlation_id},
			pluck="name",
		)
		for name in names:
			frappe.delete_doc("Wave Sync Log", name, ignore_permissions=True, delete_permanently=True)

	def test_returns_false_when_no_prior_row_exists(self):
		"""Fresh wave_id + updated_at means nothing has been processed yet."""
		self.assertFalse(is_duplicate(self.wave_id, self.updated_at))

	def test_returns_false_when_only_received_row_exists(self):
		"""A Received row alone doesn't count — only Completed rows mark the pair as processed."""
		log_step(
			self.correlation_id,
			"Received",
			"Info",
			wave_id=self.wave_id,
			wave_updated_at=self.updated_at,
		)
		self.assertFalse(is_duplicate(self.wave_id, self.updated_at))

	def test_returns_true_when_completed_row_exists(self):
		"""A Completed row for the same pair means we already did the work."""
		log_step(
			self.correlation_id,
			"Completed",
			"Success",
			wave_id=self.wave_id,
			wave_updated_at=self.updated_at,
		)
		self.assertTrue(is_duplicate(self.wave_id, self.updated_at))

	def test_returns_false_for_different_updated_at(self):
		"""A later Wave update carries a new updatedAt and must not be skipped."""
		log_step(
			self.correlation_id,
			"Completed",
			"Success",
			wave_id=self.wave_id,
			wave_updated_at="1776753292987",
		)
		self.assertFalse(is_duplicate(self.wave_id, "1776753999999"))

	def test_returns_false_when_wave_id_missing(self):
		"""Without a wave_id the pair is not well-formed; treat as non-duplicate to be safe."""
		self.assertFalse(is_duplicate(None, self.updated_at))

	def test_returns_false_when_updated_at_missing(self):
		"""Without an updated_at the pair is not well-formed; treat as non-duplicate."""
		self.assertFalse(is_duplicate(self.wave_id, None))
