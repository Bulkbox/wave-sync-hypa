"""Unit tests for the SI payment-classification backfill patch (issue #193).

The wave_payment_classification field ships as a Custom Field fixture, which Frappe
syncs in post_schema_updates — AFTER patches. On an existing site's first migrate
the column doesn't exist when this post_model_sync patch runs, so it must create the
app's fixtures first or the backfill query raises "Unknown column" (the prod 1054).
Pure-mock: frappe.db, frappe.get_all and sync_fixtures are patched at the boundary.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.patches.v1_0 import backfill_sales_invoice_payment_classification as bp


class TestBackfillSiClassificationPatch(FrappeTestCase):
	def test_syncs_fixtures_when_column_missing(self):
		# Reproduces production: column absent -> patch must create the field first.
		with (
			patch.object(frappe.db, "has_column", return_value=False),
			patch.object(bp, "sync_fixtures") as mock_sync,
			patch.object(frappe, "get_all", return_value=[]),
		):
			bp.execute()
		mock_sync.assert_called_once_with("wave_sync_hypa")

	def test_skips_fixture_sync_when_column_present(self):
		with (
			patch.object(frappe.db, "has_column", return_value=True),
			patch.object(bp, "sync_fixtures") as mock_sync,
			patch.object(frappe, "get_all", return_value=[]),
		):
			bp.execute()
		mock_sync.assert_not_called()

	def test_backfills_blank_invoice_from_source_order(self):
		invoice = frappe._dict(name="ACC-SINV-X", wave_order_id="W1")
		with (
			patch.object(frappe.db, "has_column", return_value=True),
			patch.object(bp, "sync_fixtures"),
			patch.object(frappe, "get_all", return_value=[invoice]),
			patch.object(frappe.db, "get_value", return_value="prepaid"),
			patch.object(frappe.db, "set_value") as mock_set,
		):
			bp.execute()
		mock_set.assert_called_once_with(
			"Sales Invoice", "ACC-SINV-X", "wave_payment_classification", "prepaid", update_modified=False
		)

	def test_does_not_stamp_when_source_order_unclassified(self):
		invoice = frappe._dict(name="ACC-SINV-Y", wave_order_id="W2")
		with (
			patch.object(frappe.db, "has_column", return_value=True),
			patch.object(bp, "sync_fixtures"),
			patch.object(frappe, "get_all", return_value=[invoice]),
			patch.object(frappe.db, "get_value", return_value=None),
			patch.object(frappe.db, "set_value") as mock_set,
		):
			bp.execute()
		mock_set.assert_not_called()
