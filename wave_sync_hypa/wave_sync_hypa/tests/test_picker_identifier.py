"""Unit tests for services.picker_identifier.

Both functions read the same Wave Settings.picker_identifier_source field and
branch on its three values. Tests pin the branch behaviour in isolation so we
can trust the single source of truth in production.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.services import picker_identifier as pi


def _settings(source: str = "") -> MagicMock:
	"""Wave Settings stand-in carrying just the picker_identifier_source field."""
	settings = MagicMock(name="WaveSettings")
	settings.get.side_effect = lambda key, default=None: {
		"picker_identifier_source": source,
	}.get(key, default)
	return settings


def _row(item_code: str = "JTD011", batch_no: str = "", qty: float = 0) -> SimpleNamespace:
	"""Pick List location-row stand-in carrying only the attributes the module reads."""
	return SimpleNamespace(item_code=item_code, batch_no=batch_no, qty=qty)


class TestIdentifiersForSkuOutbound(FrappeTestCase):
	"""What we send to Wave for one SKU's rows under each mode."""

	def test_blank_source_returns_distinct_batch_numbers(self):
		rows = [_row(batch_no="B-001"), _row(batch_no="B-002"), _row(batch_no="B-001")]
		out = pi.identifiers_for_sku_outbound(rows, _settings(source=""))
		self.assertEqual(out, ["B-001", "B-002"])

	def test_blank_source_drops_rows_without_batch(self):
		rows = [_row(batch_no=""), _row(batch_no="B-001"), _row(batch_no="   ")]
		out = pi.identifiers_for_sku_outbound(rows, _settings(source=""))
		self.assertEqual(out, ["B-001"])

	def test_item_code_source_returns_single_sku(self):
		rows = [_row(batch_no="B-001"), _row(batch_no="B-002")]
		out = pi.identifiers_for_sku_outbound(rows, _settings(source="Item Code"))
		self.assertEqual(out, ["JTD011"])

	def test_item_barcode_source_returns_first_barcode_row(self):
		rows = [_row(batch_no="B-001")]
		with patch.object(frappe, "get_all", return_value=[{"barcode": "5901234123457"}]):
			out = pi.identifiers_for_sku_outbound(rows, _settings(source="Item Barcode"))
		self.assertEqual(out, ["5901234123457"])

	def test_item_barcode_source_raises_when_no_barcode_present(self):
		rows = [_row(batch_no="B-001")]
		with (
			patch.object(frappe, "get_all", return_value=[]),
			patch.object(frappe, "throw", side_effect=frappe.ValidationError("missing barcode")),
		):
			with self.assertRaises(frappe.ValidationError):
				pi.identifiers_for_sku_outbound(rows, _settings(source="Item Barcode"))


class TestIdentifierMatchesInbound(FrappeTestCase):
	"""Does Wave's reported identifier match what we sent? Same three branches."""

	def test_blank_source_matches_any_allocated_batch(self):
		rows = [_row(batch_no="B-001"), _row(batch_no="B-002")]
		settings = _settings(source="")
		self.assertTrue(pi.identifier_matches_inbound("B-001", rows, settings))
		self.assertTrue(pi.identifier_matches_inbound("B-002", rows, settings))
		self.assertFalse(pi.identifier_matches_inbound("B-099", rows, settings))

	def test_item_code_source_matches_sku_only(self):
		rows = [_row(item_code="JTD011", batch_no="B-001")]
		settings = _settings(source="Item Code")
		self.assertTrue(pi.identifier_matches_inbound("JTD011", rows, settings))
		self.assertFalse(pi.identifier_matches_inbound("B-001", rows, settings))

	def test_item_barcode_source_matches_first_barcode(self):
		rows = [_row(batch_no="B-001")]
		settings = _settings(source="Item Barcode")
		with patch.object(frappe, "get_all", return_value=[{"barcode": "5901234123457"}]):
			self.assertTrue(pi.identifier_matches_inbound("5901234123457", rows, settings))
		with patch.object(frappe, "get_all", return_value=[{"barcode": "5901234123457"}]):
			self.assertFalse(pi.identifier_matches_inbound("9999999999999", rows, settings))

	def test_item_barcode_source_fails_when_no_barcode_present(self):
		"""Item lacks a barcode row but Wave reports an identifier -> mismatch (not raise)."""
		rows = [_row(batch_no="B-001")]
		settings = _settings(source="Item Barcode")
		with patch.object(frappe, "get_all", return_value=[]):
			self.assertFalse(pi.identifier_matches_inbound("anything", rows, settings))

	def test_empty_wave_id_is_treated_as_match(self):
		"""Wave didn't report an identifier -> don't flag it as a disparity in this layer."""
		rows = [_row(batch_no="B-001")]
		for source in ("", "Item Code", "Item Barcode"):
			self.assertTrue(
				pi.identifier_matches_inbound("", rows, _settings(source=source)),
				f"empty wave_id should be a match under source={source!r}",
			)


class TestCommentForSkuOutbound(FrappeTestCase):
	"""Per-product picker comment: bullet-only, deterministic, independent of source mode."""

	def test_multi_batch_produces_one_bullet_per_batch(self):
		rows = [_row(batch_no="BATCH-A", qty=3), _row(batch_no="BATCH-B", qty=2)]
		self.assertEqual(
			pi.comment_for_sku_outbound(rows),
			"- BATCH-A: 3\n- BATCH-B: 2",
		)

	def test_single_batch_produces_single_bullet(self):
		rows = [_row(batch_no="BATCH-A", qty=5)]
		self.assertEqual(pi.comment_for_sku_outbound(rows), "- BATCH-A: 5")

	def test_non_batch_tracked_renders_no_batch_tracking_line(self):
		rows = [_row(batch_no="", qty=5)]
		self.assertEqual(pi.comment_for_sku_outbound(rows), "- 5 (no batch tracking)")

	def test_mixed_batched_and_unbatched_rows(self):
		"""Rare but possible: same SKU split across batched and unbatched rows."""
		rows = [
			_row(batch_no="BATCH-A", qty=3),
			_row(batch_no="", qty=2),
		]
		self.assertEqual(
			pi.comment_for_sku_outbound(rows),
			"- BATCH-A: 3\n- 2 (no batch tracking)",
		)

	def test_empty_rows_returns_empty_string(self):
		self.assertEqual(pi.comment_for_sku_outbound([]), "")

	def test_float_quantity_renders_without_trailing_zero(self):
		"""qty=3.0 renders as '3', not '3.0'."""
		rows = [_row(batch_no="BATCH-A", qty=3.0)]
		self.assertEqual(pi.comment_for_sku_outbound(rows), "- BATCH-A: 3")

	def test_fractional_quantity_preserved(self):
		"""qty=2.5 (weighed items) renders as '2.5'."""
		rows = [_row(batch_no="BATCH-A", qty=2.5)]
		self.assertEqual(pi.comment_for_sku_outbound(rows), "- BATCH-A: 2.5")
