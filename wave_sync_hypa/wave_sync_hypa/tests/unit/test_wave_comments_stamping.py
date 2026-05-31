"""Unit tests for _apply_wave_comments — the SO stamping of Wave's order-level note."""

from __future__ import annotations

from types import SimpleNamespace

from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers import order_create as oc


def _so() -> SimpleNamespace:
	"""SO doc stand-in carrying just the wave_comments attribute the helper writes to."""
	return SimpleNamespace(wave_comments=None)


class TestApplyWaveComments(FrappeTestCase):
	def test_stamps_non_empty_comment(self):
		so = _so()
		oc._apply_wave_comments(so, {"comments": "i need the order droppe"})
		self.assertEqual(so.wave_comments, "i need the order droppe")

	def test_strips_surrounding_whitespace(self):
		so = _so()
		oc._apply_wave_comments(so, {"comments": "  ring the bell  "})
		self.assertEqual(so.wave_comments, "ring the bell")

	def test_empty_string_becomes_none(self):
		so = _so()
		oc._apply_wave_comments(so, {"comments": ""})
		self.assertIsNone(so.wave_comments)

	def test_whitespace_only_becomes_none(self):
		so = _so()
		oc._apply_wave_comments(so, {"comments": "   "})
		self.assertIsNone(so.wave_comments)

	def test_missing_key_leaves_field_none(self):
		so = _so()
		oc._apply_wave_comments(so, {})
		self.assertIsNone(so.wave_comments)
