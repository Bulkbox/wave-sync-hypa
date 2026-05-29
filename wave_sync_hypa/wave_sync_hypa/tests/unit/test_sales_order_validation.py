"""Unit tests for handlers.sales_order_validation.validate_unique_wave_order_id.

The hook enforces conditional uniqueness on wave_order_id: another SO
with the same id is OK when it's cancelled (docstatus=2), rejected
otherwise. Lets ERPNext's amend flow work without the database-level
unique constraint that previously blocked it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers.sales_order_validation import (
	validate_unique_wave_order_id,
)


def _doc(name: str = "SO-NEW", wave_order_id: str | None = "W-12345") -> SimpleNamespace:
	"""Mimic the SalesOrder doc surface the validator reads (.name, .get())."""
	doc = SimpleNamespace(name=name)
	values = {"wave_order_id": wave_order_id}
	doc.get = lambda key, default=None: values.get(key, default)
	return doc


class TestValidateUniqueWaveOrderId(FrappeTestCase):
	"""Conditional uniqueness: cancelled siblings are fine, active ones throw."""

	def test_no_wave_order_id_skips_check(self):
		"""Internal SOs with no wave_order_id are out of scope; never query."""
		with patch.object(frappe.db, "get_value") as mock_get:
			validate_unique_wave_order_id(_doc(wave_order_id=None))
			validate_unique_wave_order_id(_doc(wave_order_id=""))
			validate_unique_wave_order_id(_doc(wave_order_id="   "))
		mock_get.assert_not_called()

	def test_no_conflict_passes(self):
		"""When DB returns no matching active SO, save proceeds."""
		with patch.object(frappe.db, "get_value", return_value=None):
			# Must not raise.
			validate_unique_wave_order_id(_doc())

	def test_active_conflict_throws_naming_the_other_so(self):
		"""Another SO with the same id and docstatus < 2 → throw with the other doc's name."""
		with patch.object(frappe.db, "get_value", return_value="SO-OLD"):
			with self.assertRaises(frappe.ValidationError) as ctx:
				validate_unique_wave_order_id(_doc())
		self.assertIn("SO-OLD", str(ctx.exception))
		self.assertIn("W-12345", str(ctx.exception))

	def test_filter_excludes_self_and_cancelled_siblings(self):
		"""Verify the DB filter shape: exclude self by name + docstatus < 2 (cancelled allowed)."""
		captured: dict = {}

		def capture(doctype, filters=None, fieldname=None):
			captured["filters"] = filters
			return None

		with patch.object(frappe.db, "get_value", side_effect=capture):
			validate_unique_wave_order_id(_doc(name="SO-NEW", wave_order_id="W-12345"))

		filters = captured["filters"]
		self.assertEqual(filters["wave_order_id"], "W-12345")
		self.assertEqual(filters["name"], ["!=", "SO-NEW"])
		# Critical: docstatus filter must allow cancelled (=2) siblings — only
		# 0 (draft) and 1 (submitted) are conflicts.
		self.assertEqual(filters["docstatus"], ["<", 2])
