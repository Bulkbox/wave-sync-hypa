"""Backfill Sales Invoice.wave_payment_classification from the source Sales Order.

The SI mirror field is new (issue #193). Invoices whose wave_order_id was stamped
by an earlier feature carry a blank classification, so the "Wave Payment Entry"
button — which gates on it — would not show for them after the feature is enabled.
Stamp it from the source order's classification.

Idempotent: only fills blanks, and runs from after_install too (install marks
patches done-without-running). Safe to re-run.
"""

from __future__ import annotations

import frappe
from frappe.utils.fixtures import sync_fixtures


def execute():
	# wave_payment_classification ships as a Custom Field fixture, which Frappe
	# syncs in post_schema_updates — AFTER this post_model_sync patch. On an existing
	# site seeing the field for the first time the column doesn't exist yet, so the
	# backfill query below would raise "Unknown column". Create the app's fixtures
	# now (idempotent; the later post_schema_updates sync is then a no-op).
	if not frappe.db.has_column("Sales Invoice", "wave_payment_classification"):
		sync_fixtures("wave_sync_hypa")

	invoices = frappe.get_all(
		"Sales Invoice",
		filters={"wave_order_id": ["is", "set"], "wave_payment_classification": ["in", ["", None]]},
		fields=["name", "wave_order_id"],
	)
	for si in invoices:
		classification = frappe.db.get_value(
			"Sales Order", {"wave_order_id": si.wave_order_id}, "wave_payment_classification"
		)
		if classification:
			frappe.db.set_value(
				"Sales Invoice", si.name, "wave_payment_classification", classification, update_modified=False
			)
