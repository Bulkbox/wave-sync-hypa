"""Unit tests for the item-resolution soft-fail path and the placeholder fallback."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers import order_create as oc
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveResolutionError


def _so() -> SimpleNamespace:
	"""SO doc stand-in with the minimum surface _append_product_lines and friends touch."""
	doc = SimpleNamespace()
	doc.items = []
	doc.delivery_date = "2026-05-16"
	doc.append = lambda field, row: doc.items.append(row)
	return doc


def _product(sku: str, qty: int = 1) -> dict:
	"""Minimal Wave product dict."""
	return {"sku": sku, "quantity": qty, "productId": f"wp-{sku}"}


class TestAppendProductLines(FrappeTestCase):
	"""_append_product_lines now returns a list of unresolved entries instead of raising."""

	def test_all_resolve_returns_empty_skipped(self):
		so = _so()
		with (
			patch.object(oc, "resolve_sku", side_effect=lambda s: f"ITEM-{s}"),
			patch.object(oc, "_log_items_resolved"),
		):
			skipped = oc._append_product_lines(
				so, {"products": [_product("BFK162"), _product("JTD011")]}, "corr-1"
			)
		self.assertEqual(skipped, [])
		self.assertEqual([row["item_code"] for row in so.items], ["ITEM-BFK162", "ITEM-JTD011"])

	def test_some_resolve_others_captured(self):
		so = _so()

		def fake_resolve(sku):
			if sku == "MISSING":
				raise WaveResolutionError(f"no item with sku {sku!r}")
			return f"ITEM-{sku}"

		with (
			patch.object(oc, "resolve_sku", side_effect=fake_resolve),
			patch.object(oc, "_log_items_resolved"),
		):
			skipped = oc._append_product_lines(
				so,
				{"products": [_product("BFK162", qty=2), _product("MISSING", qty=3)]},
				"corr-2",
			)
		self.assertEqual([row["item_code"] for row in so.items], ["ITEM-BFK162"])
		self.assertEqual(len(skipped), 1)
		self.assertEqual(skipped[0]["sku"], "MISSING")
		self.assertEqual(skipped[0]["quantity"], 3)
		self.assertEqual(skipped[0]["wave_product_id"], "wp-MISSING")
		self.assertIn("no item with sku", skipped[0]["error"])

	def test_all_unresolved_returns_full_list_no_lines_appended(self):
		so = _so()
		with (
			patch.object(oc, "resolve_sku", side_effect=WaveResolutionError("nope")),
			patch.object(oc, "_log_items_resolved"),
		):
			skipped = oc._append_product_lines(
				so, {"products": [_product("A"), _product("B")]}, "corr-3"
			)
		self.assertEqual(so.items, [])
		self.assertEqual(len(skipped), 2)
		self.assertEqual({e["sku"] for e in skipped}, {"A", "B"})

	def test_empty_products_array(self):
		so = _so()
		with patch.object(oc, "_log_items_resolved"):
			skipped = oc._append_product_lines(so, {"products": []}, "corr-4")
		self.assertEqual(skipped, [])
		self.assertEqual(so.items, [])


class TestAppendPlaceholderForUnresolved(FrappeTestCase):
	"""The single-line fallback used when zero items resolve."""

	def test_appends_when_placeholder_configured_and_exists(self):
		so = _so()
		settings = MagicMock(name="WaveSettings")
		settings.get.side_effect = lambda key, default=None: {
			"default_unresolved_items_placeholder": "Wave Unresolved Placeholder",
		}.get(key, default)
		with patch.object(frappe.db, "exists", return_value=True):
			result = oc._append_placeholder_for_unresolved(so, settings)
		self.assertTrue(result)
		self.assertEqual(len(so.items), 1)
		self.assertEqual(so.items[0]["item_code"], "Wave Unresolved Placeholder")
		self.assertEqual(so.items[0]["qty"], 1)
		self.assertEqual(so.items[0]["rate"], 0)
		self.assertIn("no resolvable items", so.items[0]["description"])

	def test_returns_false_when_placeholder_setting_is_blank(self):
		so = _so()
		settings = MagicMock(name="WaveSettings")
		settings.get.return_value = ""
		result = oc._append_placeholder_for_unresolved(so, settings)
		self.assertFalse(result)
		self.assertEqual(so.items, [])

	def test_returns_false_when_placeholder_item_does_not_exist(self):
		"""Misconfiguration: setting points at a non-existent Item code."""
		so = _so()
		settings = MagicMock(name="WaveSettings")
		settings.get.return_value = "GhostItem"
		with patch.object(frappe.db, "exists", return_value=False):
			result = oc._append_placeholder_for_unresolved(so, settings)
		self.assertFalse(result)
		self.assertEqual(so.items, [])


class TestAbortIntakeNoPlaceholder(FrappeTestCase):
	"""The abort path: zero items resolved AND no placeholder Item configured."""

	def test_writes_aborted_log_row_and_frappe_error_log(self):
		skipped = [{"sku": "AAA"}, {"sku": "BBB"}]
		with (
			patch.object(oc, "log_step") as mock_log,
			patch.object(frappe, "log_error") as mock_frappe_log,
		):
			oc._abort_intake_no_placeholder(
				{"_id": "w-1", "friendlyId": "10000070"},
				"corr-5",
				skipped,
			)
		mock_log.assert_called_once()
		# First positional arg is the correlation id, second is the step name.
		self.assertEqual(mock_log.call_args.args[0], "corr-5")
		self.assertEqual(mock_log.call_args.args[1], "Aborted")
		self.assertEqual(mock_log.call_args.args[2], "Error")
		# Both unresolved SKUs are named in the error message for triage.
		err = mock_log.call_args.kwargs["error_message"]
		self.assertIn("AAA", err)
		self.assertIn("BBB", err)
		# Frappe Error Log row is written so it surfaces in the desk's standard view.
		mock_frappe_log.assert_called_once()
