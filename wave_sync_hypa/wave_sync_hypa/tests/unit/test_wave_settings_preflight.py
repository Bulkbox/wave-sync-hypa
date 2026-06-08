"""Unit tests for WaveSettings._validate_intake_defaults (issue #149).

Save-time pre-flight: a configured order-intake default that points at an
unusable record blocks the save; when enabled, the five core defaults must
also be present. frappe.db lookups are mocked so no real records are needed.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

# Valid shapes returned by frappe.db.get_value for each looked-up doctype.
_VALID = {
	"Warehouse": frappe._dict(is_group=0, disabled=0, company="C"),
	"Price List": frappe._dict(enabled=1, selling=1),
	"Currency": frappe._dict(enabled=1),
	"Customer": frappe._dict(disabled=0),
}


def _doc(**overrides):
	values = {
		"doctype": "Wave Settings",
		"enabled": 0,
		"default_company": "C",
		"default_warehouse": "WH",
		"default_price_list": "PL",
		"default_currency": "KES",
		"walk_in_customer": "WALK",
	}
	values.update(overrides)
	return frappe.get_doc(values)


def _run(doc, *, db_overrides=None, company_exists=True):
	"""Call the validator with mocked frappe.db; db_overrides replaces a doctype's row."""
	rows = {**_VALID, **(db_overrides or {})}
	with (
		patch.object(frappe.db, "exists", return_value=company_exists),
		patch.object(frappe.db, "get_value", side_effect=lambda dt, *a, **k: rows.get(dt)),
	):
		doc._validate_intake_defaults()


class TestConfigPreflight(FrappeTestCase):
	"""Block unusable intake defaults at save; require the core five when enabled."""

	def test_valid_config_disabled_passes(self):
		_run(_doc())  # no throw

	def test_valid_config_enabled_passes(self):
		_run(_doc(enabled=1))  # all present + valid

	def test_enabled_with_blank_warehouse_raises(self):
		with self.assertRaises(frappe.ValidationError):
			_run(_doc(enabled=1, default_warehouse=""))

	def test_disabled_with_blank_warehouse_passes(self):
		_run(_doc(enabled=0, default_warehouse=""))  # blanks allowed mid-setup

	def test_missing_warehouse_raises(self):
		with self.assertRaises(frappe.ValidationError):
			_run(_doc(), db_overrides={"Warehouse": None})

	def test_group_warehouse_raises(self):
		with self.assertRaises(frappe.ValidationError):
			_run(_doc(), db_overrides={"Warehouse": frappe._dict(is_group=1, disabled=0, company="C")})

	def test_disabled_warehouse_raises(self):
		with self.assertRaises(frappe.ValidationError):
			_run(_doc(), db_overrides={"Warehouse": frappe._dict(is_group=0, disabled=1, company="C")})

	def test_warehouse_wrong_company_raises(self):
		with self.assertRaises(frappe.ValidationError):
			_run(_doc(), db_overrides={"Warehouse": frappe._dict(is_group=0, disabled=0, company="OTHER")})

	def test_non_selling_price_list_raises(self):
		with self.assertRaises(frappe.ValidationError):
			_run(_doc(), db_overrides={"Price List": frappe._dict(enabled=1, selling=0)})

	def test_disabled_currency_raises(self):
		with self.assertRaises(frappe.ValidationError):
			_run(_doc(), db_overrides={"Currency": frappe._dict(enabled=0)})

	def test_disabled_walk_in_raises(self):
		with self.assertRaises(frappe.ValidationError):
			_run(_doc(), db_overrides={"Customer": frappe._dict(disabled=1)})

	def test_missing_walk_in_raises(self):
		with self.assertRaises(frappe.ValidationError):
			_run(_doc(), db_overrides={"Customer": None})

	def test_missing_company_raises(self):
		with self.assertRaises(frappe.ValidationError):
			_run(_doc(), company_exists=False)
