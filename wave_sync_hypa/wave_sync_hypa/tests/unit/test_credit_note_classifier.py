"""Unit tests for services/credit_note_classifier.is_full_value_credit_note.

Classifier is the only deciding mechanism for full-value vs partial credit
notes — when this returns True we push CANCELLED to Wave; when False we
keep the Wave order at UNDER_DELIVERY. So the boundary cases (rounding,
missing source, malformed return) are all pinned here.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.services import credit_note_classifier


def _doc(*, is_return=0, return_against="", grand_total=0.0) -> SimpleNamespace:
	"""Build a Sales Invoice stand-in with .get(...) the classifier reads."""
	values = {
		"is_return": is_return,
		"return_against": return_against,
		"grand_total": grand_total,
	}
	d = SimpleNamespace()
	d.get = lambda key, default=None: values.get(key, default)
	return d


class TestIsFullValueCreditNote(FrappeTestCase):
	"""Pure-function comparison of credit grand_total to original SI grand_total."""

	def test_returns_false_for_non_return_invoice(self):
		doc = _doc(is_return=0, return_against="SI-ORIG", grand_total=-100.0)
		with patch.object(frappe.db, "get_value") as mock_get:
			self.assertFalse(credit_note_classifier.is_full_value_credit_note(doc))
		# Did not even need to look up the source.
		mock_get.assert_not_called()

	def test_returns_false_when_return_against_missing(self):
		"""Malformed credit note (is_return=1 but no return_against) -> False, defensive."""
		doc = _doc(is_return=1, return_against="", grand_total=-100.0)
		with patch.object(frappe.db, "get_value") as mock_get:
			self.assertFalse(credit_note_classifier.is_full_value_credit_note(doc))
		mock_get.assert_not_called()

	def test_returns_false_when_source_invoice_not_found(self):
		"""Source SI deleted or never persisted -> classifier conservatively returns False."""
		doc = _doc(is_return=1, return_against="SI-MISSING", grand_total=-200.0)
		with patch.object(frappe.db, "get_value", return_value=None):
			self.assertFalse(credit_note_classifier.is_full_value_credit_note(doc))

	def test_returns_true_for_exact_match(self):
		"""abs(credit) == original -> full-value."""
		doc = _doc(is_return=1, return_against="SI-ORIG", grand_total=-1234.56)
		with patch.object(frappe.db, "get_value", return_value=1234.56):
			self.assertTrue(credit_note_classifier.is_full_value_credit_note(doc))

	def test_returns_true_within_one_cent_tolerance(self):
		"""1234.55 vs 1234.56 (sub-cent rounding drift) still counts as full-value."""
		doc = _doc(is_return=1, return_against="SI-ORIG", grand_total=-1234.555)
		with patch.object(frappe.db, "get_value", return_value=1234.56):
			self.assertTrue(credit_note_classifier.is_full_value_credit_note(doc))

	def test_returns_false_just_outside_tolerance(self):
		"""Drift larger than the 1-cent tolerance reads as partial."""
		doc = _doc(is_return=1, return_against="SI-ORIG", grand_total=-1234.50)
		with patch.object(frappe.db, "get_value", return_value=1234.56):
			self.assertFalse(credit_note_classifier.is_full_value_credit_note(doc))

	def test_returns_false_for_partial_return(self):
		"""abs(credit) < original / 2 -> partial."""
		doc = _doc(is_return=1, return_against="SI-ORIG", grand_total=-200.0)
		with patch.object(frappe.db, "get_value", return_value=1000.0):
			self.assertFalse(credit_note_classifier.is_full_value_credit_note(doc))

	def test_handles_positive_credit_total_defensively(self):
		"""ERPNext signs credit notes negative, but if a row is positive (data anomaly), abs() handles it."""
		doc = _doc(is_return=1, return_against="SI-ORIG", grand_total=1234.56)
		with patch.object(frappe.db, "get_value", return_value=1234.56):
			self.assertTrue(credit_note_classifier.is_full_value_credit_note(doc))
