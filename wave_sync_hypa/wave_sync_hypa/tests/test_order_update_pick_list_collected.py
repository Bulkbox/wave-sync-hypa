"""Unit tests for handlers.order_update.

Pure unit tests: every Frappe DB call and `frappe.get_doc` / `frappe.get_cached_doc`
is patched so we exercise the branching logic in isolation. The wiring of the
inbound flag through to the existing permission gate (handlers.pick_list) has its
own integration test in test_pick_list_submit_gate; here we only assert that the
flag is set during submit and cleared in the finally clause.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers import order_update as ou


def _settings(enabled: int = 1, picker_identifier_source: str = "") -> MagicMock:
	"""Wave Settings stand-in: master switch + picker_identifier_source mode."""
	values = {
		"pick_list_inbound_submit_enabled": enabled,
		"picker_identifier_source": picker_identifier_source,
	}
	settings = MagicMock(name="WaveSettings")
	settings.get.side_effect = lambda key, default=None: values.get(key, default)
	return settings


def _location(
	item_code: str,
	sales_order: str = "",
	picked_qty: float = 0,
	batch_no: str = "",
	qty: float = 0,
) -> SimpleNamespace:
	"""Pick List location-row stand-in matching the attributes the handler touches."""
	return SimpleNamespace(
		item_code=item_code,
		sales_order=sales_order,
		picked_qty=picked_qty,
		batch_no=batch_no,
		qty=qty,
	)


def _pick_list(name: str = "PICK-2026-99999", docstatus: int = 0, locations=None) -> MagicMock:
	"""Pick List doc stand-in carrying just the surface the handler reads."""
	doc = MagicMock(name="PickListDoc")
	doc.name = name
	doc.doctype = "Pick List"
	doc.docstatus = docstatus
	doc.locations = locations or []
	doc.flags = SimpleNamespace()
	return doc


def _payload(**overrides) -> dict:
	"""Build a realistic ORDER.UPDATE payload modelled on the live JTD011 example."""
	base = {
		"_id": "6a06c08305c378eb94cdc603",
		"friendlyId": "10000070",
		"pickerStatus": "COLLECTED",
		"comments": "",
		"products": [
			{"productId": "wp-JTD011", "sku": "JTD011", "batchIds": ["JTD01100016"]},
		],
		"picking": {
			"completedAt": "2026-05-15T07:18:39.420Z",
			"assignedToUser": {
				"firstName": "Hypa", "lastName": "Picker 1",
				"email": "hypapicker1@wavegrocery.com",
			},
			"items": [
				{"productId": "wp-JTD011", "quantity": 2, "status": "COLLECTED", "replacements": []},
			],
		},
	}
	base.update(overrides)
	return base


class TestTriggerFilter(FrappeTestCase):
	"""Only pickerStatus=COLLECTED triggers any work."""

	def test_other_picker_status_logs_and_returns_without_dispatch(self):
		with (
			patch.object(frappe, "get_cached_doc") as mock_gcd,
			patch.object(frappe, "get_all") as mock_ga,
			patch.object(ou, "log_step") as mock_log,
		):
			ou.handle(_payload(pickerStatus="PICKING"), "corr-1")
		mock_gcd.assert_not_called()
		mock_ga.assert_not_called()
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertEqual(steps, [ou.STEP_NOT_COLLECTED])

	def test_collected_with_kill_switch_off_logs_and_returns(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(enabled=0)),
			patch.object(frappe, "get_all") as mock_ga,
			patch.object(ou, "log_step") as mock_log,
		):
			ou.handle(_payload(), "corr-2")
		mock_ga.assert_not_called()
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertEqual(steps, [ou.STEP_DISABLED])

	def test_no_matching_pick_list_logs_warning_and_exits(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "get_all", return_value=[]),
			patch.object(frappe, "get_doc") as mock_get_doc,
			patch.object(ou, "log_step") as mock_log,
		):
			ou.handle(_payload(), "corr-3")
		mock_get_doc.assert_not_called()
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertEqual(steps, [ou.STEP_NO_PICK_LIST])


class TestDraftPickList(FrappeTestCase):
	"""docstatus = 0 → update + comment + submit; replacements suppress submit."""

	def test_clean_draft_updates_lines_and_submits(self):
		# ERPNext-allocated row already carries the batch_no Wave reports back.
		pl = _pick_list(locations=[_location("JTD011", sales_order="SO-X", qty=2, batch_no="JTD01100016")])
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "get_all", return_value=["PICK-X"]),
			patch.object(frappe, "get_doc", return_value=pl),
			patch.object(ou, "log_step") as mock_log,
		):
			ou.handle(_payload(), "corr-4")
		# picked_qty was greedy-filled from Wave's quantity.
		self.assertEqual(pl.locations[0].picked_qty, 2)
		# row.batch_no is NEVER overwritten — ERPNext's FEFO allocation is authoritative.
		self.assertEqual(pl.locations[0].batch_no, "JTD01100016")
		# Save + submit fired exactly once each.
		pl.save.assert_called()
		pl.submit.assert_called_once()
		# Picker audit Comment was added.
		comment_bodies = [c.args[1] for c in pl.add_comment.call_args_list]
		self.assertTrue(any("Picked by Hypa Picker 1" in b for b in comment_bodies))
		# Inbound flag was cleared after submit.
		self.assertFalse(frappe.flags.get("wave_inbound_pick_list_submit"))
		# Success row recorded.
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertIn(ou.STEP_DRAFT_SUBMITTED, steps)

	def test_replacement_suppresses_submit_and_logs_warning(self):
		pl = _pick_list(locations=[_location("JTD011", sales_order="SO-X", qty=2, batch_no="JTD01100016")])
		payload = _payload()
		payload["picking"]["items"][0]["replacements"] = [
			{"withProductId": "wp-SUB", "quantity": 1, "pending": False},
		]
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "get_all", return_value=["PICK-X"]),
			patch.object(frappe, "get_doc", return_value=pl),
			patch.object(ou, "log_step") as mock_log,
		):
			ou.handle(payload, "corr-5")
		pl.submit.assert_not_called()
		# Save still happened so comment + line edits persist.
		pl.save.assert_called()
		comment_bodies = [c.args[1] for c in pl.add_comment.call_args_list]
		self.assertTrue(any("substituted SKU JTD011 with productId wp-SUB" in b for b in comment_bodies))
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertIn(ou.STEP_REPLACEMENT_PRESENT, steps)
		self.assertNotIn(ou.STEP_DRAFT_SUBMITTED, steps)

	def test_removed_item_zeroes_qty_and_blocks_submit(self):
		"""REMOVED is now a disparity — operator decides whether to amend / resubmit."""
		pl = _pick_list(locations=[_location("JTD011", sales_order="SO-X", qty=2, batch_no="JTD01100016")])
		payload = _payload()
		payload["picking"]["items"][0]["status"] = "REMOVED"
		payload["picking"]["items"][0]["quantity"] = 0
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "get_all", return_value=["PICK-X"]),
			patch.object(frappe, "get_doc", return_value=pl),
			patch.object(ou, "log_step") as mock_log,
		):
			ou.handle(payload, "corr-6")
		self.assertEqual(pl.locations[0].picked_qty, 0)
		pl.submit.assert_not_called()
		pl.save.assert_called()
		comment_bodies = [c.args[1] for c in pl.add_comment.call_args_list]
		self.assertTrue(any("REMOVED" in b for b in comment_bodies))
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertIn(ou.STEP_DISPARITY_PRESENT, steps)
		self.assertNotIn(ou.STEP_DRAFT_SUBMITTED, steps)

	def test_batch_identifier_mismatch_blocks_submit(self):
		"""Wave reports a batch the ERP row doesn't carry — identifier mismatch, no submit."""
		# ERP allocated batch-A; Wave reports batch-B and batch-C.
		pl = _pick_list(locations=[_location("JTD011", sales_order="SO-X", qty=2, batch_no="BATCH-A")])
		payload = _payload()
		payload["products"][0]["batchIds"] = ["BATCH-B", "BATCH-C"]
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "get_all", return_value=["PICK-X"]),
			patch.object(frappe, "get_doc", return_value=pl),
			patch.object(ou, "log_step") as mock_log,
		):
			ou.handle(payload, "corr-7")
		# row.batch_no is untouched; ERPNext's allocation is preserved.
		self.assertEqual(pl.locations[0].batch_no, "BATCH-A")
		pl.submit.assert_not_called()
		pl.save.assert_called()
		comment_bodies = [c.args[1] for c in pl.add_comment.call_args_list]
		self.assertTrue(any("identifier mismatch" in b for b in comment_bodies))
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertIn(ou.STEP_DISPARITY_PRESENT, steps)

	def test_sku_in_wave_but_not_in_pick_list_blocks_submit(self):
		"""SKU present in Wave but not in ERP PL -> missing-in-ERP disparity -> no submit."""
		pl = _pick_list(locations=[_location("OTHER", sales_order="SO-X", qty=1, batch_no="OTHER-001")])
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "get_all", return_value=["PICK-X"]),
			patch.object(frappe, "get_doc", return_value=pl),
			patch.object(ou, "log_step") as mock_log,
		):
			ou.handle(_payload(), "corr-8")
		comment_bodies = [c.args[1] for c in pl.add_comment.call_args_list]
		self.assertTrue(any("no matching line" in b for b in comment_bodies))
		pl.submit.assert_not_called()
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertIn(ou.STEP_DISPARITY_PRESENT, steps)

	def test_inbound_flag_cleared_even_when_submit_raises(self):
		pl = _pick_list(locations=[_location("JTD011", qty=2, batch_no="JTD01100016")])
		pl.submit.side_effect = RuntimeError("boom")
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "get_all", return_value=["PICK-X"]),
			patch.object(frappe, "get_doc", return_value=pl),
			patch.object(frappe.db, "rollback"),
			patch.object(frappe, "get_traceback", return_value=""),
			patch.object(ou, "log_step") as mock_log,
		):
			ou.handle(_payload(), "corr-9")
		# Both flags must be cleared regardless of submit failure.
		self.assertFalse(frappe.flags.get("wave_inbound_pick_list_submit"))
		self.assertFalse(frappe.flags.get("ignore_permissions"))
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertIn(ou.STEP_SUBMIT_FAILED, steps)

	def test_global_ignore_permissions_set_during_submit(self):
		"""ERPNext's Pick List on_submit creates a nested Serial and Batch Bundle whose
		permission check consults frappe.flags.ignore_permissions. Pin that we set it."""
		pl = _pick_list(locations=[_location("JTD011", qty=2, batch_no="JTD01100016")])
		seen_during_submit: dict[str, bool] = {}

		def capture_flag_then_succeed():
			seen_during_submit["ignore_permissions"] = bool(frappe.flags.get("ignore_permissions"))

		pl.submit.side_effect = capture_flag_then_succeed
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "get_all", return_value=["PICK-X"]),
			patch.object(frappe, "get_doc", return_value=pl),
			patch.object(ou, "log_step"),
		):
			ou.handle(_payload(), "corr-9b")
		self.assertTrue(seen_during_submit["ignore_permissions"],
			"frappe.flags.ignore_permissions must be set during the inbound submit so nested "
			"doc creates (Serial and Batch Bundle, etc.) pass their permission checks.")
		# Cleared after submit.
		self.assertFalse(frappe.flags.get("ignore_permissions"))


class TestTerminalPickList(FrappeTestCase):
	"""docstatus = 1 or 2 → comment only, never modify state."""

	def test_submitted_pick_list_only_adds_summary_comment(self):
		pl = _pick_list(docstatus=1, locations=[_location("JTD011", sales_order="SO-X", picked_qty=99)])
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "get_all", return_value=["PICK-X"]),
			patch.object(frappe, "get_doc", return_value=pl),
			patch.object(ou, "log_step") as mock_log,
		):
			ou.handle(_payload(), "corr-10")
		# Line state untouched.
		self.assertEqual(pl.locations[0].picked_qty, 99)
		self.assertEqual(pl.locations[0].batch_no, "")
		# No submit, no save.
		pl.submit.assert_not_called()
		pl.save.assert_not_called()
		# A Comment WAS added with Wave's reported pick state.
		pl.add_comment.assert_called()
		bodies = [c.args[1] for c in pl.add_comment.call_args_list]
		self.assertTrue(any("Wave reported picking-complete" in b for b in bodies))
		self.assertTrue(any("SKU JTD011" in b for b in bodies))
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertEqual(steps, [ou.STEP_ANNOTATED_SUBMITTED])

	def test_cancelled_pick_list_only_adds_summary_comment(self):
		pl = _pick_list(docstatus=2, locations=[_location("JTD011", sales_order="SO-X", picked_qty=55)])
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "get_all", return_value=["PICK-X"]),
			patch.object(frappe, "get_doc", return_value=pl),
			patch.object(ou, "log_step") as mock_log,
		):
			ou.handle(_payload(), "corr-11")
		self.assertEqual(pl.locations[0].picked_qty, 55)
		pl.submit.assert_not_called()
		pl.save.assert_not_called()
		pl.add_comment.assert_called()
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertEqual(steps, [ou.STEP_ANNOTATED_CANCELLED])


class TestCustomerCommentPropagation(FrappeTestCase):
	"""Customer comment lands as 'Customer now asks: ...' on PL and linked SO."""

	def test_propagates_to_pick_list_and_sales_order_when_present(self):
		pl = _pick_list(locations=[_location("JTD011", sales_order="SO-A", qty=2, batch_no="JTD01100016")])
		so = MagicMock(name="SalesOrder")
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "get_all", return_value=["PICK-X"]),
			patch.object(frappe, "get_doc", side_effect=[pl, so]),
			patch.object(ou, "log_step"),
		):
			ou.handle(_payload(comments="i need the order droppe"), "corr-12")
		pl_comment_bodies = [c.args[1] for c in pl.add_comment.call_args_list]
		self.assertTrue(
			any(b == "Customer now asks: i need the order droppe" for b in pl_comment_bodies),
			f"Expected the customer-now-asks comment on PL; got {pl_comment_bodies!r}",
		)
		so_comment_bodies = [c.args[1] for c in so.add_comment.call_args_list]
		self.assertEqual(so_comment_bodies, ["Customer now asks: i need the order droppe"])

	def test_empty_comment_adds_nothing(self):
		pl = _pick_list(locations=[_location("JTD011", sales_order="SO-A", qty=2, batch_no="JTD01100016")])
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "get_all", return_value=["PICK-X"]),
			patch.object(frappe, "get_doc", return_value=pl),
			patch.object(ou, "log_step"),
		):
			ou.handle(_payload(comments=""), "corr-13")
		comment_bodies = [c.args[1] for c in pl.add_comment.call_args_list]
		self.assertFalse(
			any("Customer now asks" in b for b in comment_bodies),
			"No customer-now-asks comment should be added for an empty Wave comment.",
		)


class TestReconciliationAllocator(FrappeTestCase):
	"""Per-SKU multi-row reconciliation: greedy fill, verdicts, no batch_no rewrite."""

	def _doc(self, locations: list) -> SimpleNamespace:
		"""Doc stand-in carrying only the locations[] the allocator walks."""
		return SimpleNamespace(locations=locations)

	def _wave(self, quantity: float, batch_ids=None, status: str = "COLLECTED") -> dict:
		"""Minimal Wave picking-index entry for one SKU."""
		return {
			"quantity": quantity,
			"batch_ids": list(batch_ids or []),
			"status": status,
			"replacements": [],
			"wave_product_id": "wp-X",
		}

	def test_multi_row_clean_fills_greedy_no_disparity(self):
		# ERPNext split 5 units across two batches; Wave reports 5 picked from the first.
		rows = [
			_location("JTD011", qty=3, batch_no="BATCH-A"),
			_location("JTD011", qty=2, batch_no="BATCH-B"),
		]
		doc = self._doc(rows)
		index = {"JTD011": self._wave(5, batch_ids=["BATCH-A"])}
		outcome = ou._apply_wave_picking_to_locations(doc, index, _settings())
		self.assertEqual([r.picked_qty for r in rows], [3, 2])
		self.assertFalse(outcome.has_disparity)
		self.assertEqual(outcome.anomalies, [])
		# batch_no untouched.
		self.assertEqual([r.batch_no for r in rows], ["BATCH-A", "BATCH-B"])

	def test_multi_row_shortfall_partial_fill_disparity(self):
		# ERP allocated 5; Wave reports only 4 picked.
		rows = [
			_location("JTD011", qty=3, batch_no="BATCH-A"),
			_location("JTD011", qty=2, batch_no="BATCH-B"),
		]
		outcome = ou._apply_wave_picking_to_locations(
			self._doc(rows),
			{"JTD011": self._wave(4, batch_ids=["BATCH-A"])},
			_settings(),
		)
		# Greedy: first row gets 3, second gets 1, remaining stays 0.
		self.assertEqual([r.picked_qty for r in rows], [3, 1])
		self.assertTrue(outcome.has_disparity)
		self.assertEqual(len(outcome.anomalies), 1)
		self.assertIn("shortfall", outcome.anomalies[0])

	def test_multi_row_overpick_caps_and_flags_disparity(self):
		rows = [
			_location("JTD011", qty=3, batch_no="BATCH-A"),
			_location("JTD011", qty=2, batch_no="BATCH-B"),
		]
		outcome = ou._apply_wave_picking_to_locations(
			self._doc(rows),
			{"JTD011": self._wave(7, batch_ids=["BATCH-A"])},
			_settings(),
		)
		# Capped at 5 across rows; both rows filled.
		self.assertEqual([r.picked_qty for r in rows], [3, 2])
		self.assertTrue(outcome.has_disparity)
		self.assertIn("overpick", outcome.anomalies[0])

	def test_batch_mode_identifier_mismatch_flags_disparity(self):
		rows = [_location("JTD011", qty=2, batch_no="BATCH-A")]
		outcome = ou._apply_wave_picking_to_locations(
			self._doc(rows),
			{"JTD011": self._wave(2, batch_ids=["BATCH-Z"])},
			_settings(),  # blank source = batch mode
		)
		self.assertTrue(outcome.has_disparity)
		self.assertIn("identifier mismatch", outcome.anomalies[0])

	def test_barcode_mode_identifier_match_no_disparity(self):
		rows = [_location("JTD011", qty=2, batch_no="BATCH-A")]
		with patch.object(frappe, "get_all", return_value=[{"barcode": "5901234123457"}]):
			outcome = ou._apply_wave_picking_to_locations(
				self._doc(rows),
				{"JTD011": self._wave(2, batch_ids=["5901234123457"])},
				_settings(picker_identifier_source="Item Barcode"),
			)
		self.assertFalse(outcome.has_disparity)

	def test_barcode_mode_identifier_mismatch_flags_disparity(self):
		rows = [_location("JTD011", qty=2, batch_no="BATCH-A")]
		with patch.object(frappe, "get_all", return_value=[{"barcode": "5901234123457"}]):
			outcome = ou._apply_wave_picking_to_locations(
				self._doc(rows),
				{"JTD011": self._wave(2, batch_ids=["9999999999999"])},
				_settings(picker_identifier_source="Item Barcode"),
			)
		self.assertTrue(outcome.has_disparity)
		self.assertIn("identifier mismatch", outcome.anomalies[0])

	def test_item_code_mode_match_no_disparity(self):
		rows = [_location("JTD011", qty=2, batch_no="BATCH-A")]
		outcome = ou._apply_wave_picking_to_locations(
			self._doc(rows),
			{"JTD011": self._wave(2, batch_ids=["JTD011"])},  # Wave echoes the SKU
			_settings(picker_identifier_source="Item Code"),
		)
		self.assertFalse(outcome.has_disparity)

	def test_removed_zeroes_qty_and_flags_disparity(self):
		rows = [
			_location("JTD011", qty=2, batch_no="BATCH-A"),
			_location("JTD011", qty=1, batch_no="BATCH-B"),
		]
		outcome = ou._apply_wave_picking_to_locations(
			self._doc(rows),
			{"JTD011": self._wave(0, status="REMOVED")},
			_settings(),
		)
		self.assertEqual([r.picked_qty for r in rows], [0.0, 0.0])
		self.assertTrue(outcome.has_disparity)
		self.assertIn("REMOVED", outcome.anomalies[0])

	def test_missing_in_erp_flags_disparity(self):
		# Wave reports a SKU the PL doesn't carry at all.
		outcome = ou._apply_wave_picking_to_locations(
			self._doc([_location("OTHER", qty=1, batch_no="B-1")]),
			{"JTD011": self._wave(2, batch_ids=["BATCH-A"])},
			_settings(),
		)
		self.assertTrue(outcome.has_disparity)
		self.assertTrue(any("no matching line" in a for a in outcome.anomalies))
