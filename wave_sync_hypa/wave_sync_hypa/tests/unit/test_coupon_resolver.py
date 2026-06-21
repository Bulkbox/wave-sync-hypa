"""Unit tests for resolvers.coupon_resolver.

frappe.db lookups, frappe.get_doc inserts and log_step are patched at the
boundary so the create / realign / mismatch branches are exercised without a DB.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.resolvers import coupon_resolver as cr
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveResolutionError


def _settings(apply_on: str = "Grand Total", currency: str = "KES") -> MagicMock:
	s = MagicMock(name="WaveSettings")
	s.get.side_effect = lambda key, default=None: {
		"coupon_apply_discount_on": apply_on,
		"default_currency": currency,
	}.get(key, default)
	return s


class TestCreate(FrappeTestCase):
	def test_creates_pricing_rule_then_coupon_when_missing(self):
		specs = []

		def fake_get_doc(spec):
			doc = MagicMock()
			doc.name = spec.get("coupon_name") or spec.get("title")
			doc.insert.return_value = doc
			specs.append(spec)
			return doc

		with (
			patch.object(frappe.db, "get_value", return_value=None),  # no existing coupon
			patch.object(frappe, "get_doc", side_effect=fake_get_doc),
			patch.object(cr, "log_step"),
		):
			name = cr.resolve_coupon("HYPA10", 10.0, _settings(), "corr-1")

		self.assertEqual(name, "HYPA10")
		rule_spec, coupon_spec = specs
		# Pricing Rule: fixed-amount, transaction-level, coupon-gated.
		self.assertEqual(rule_spec["apply_on"], "Transaction")
		self.assertEqual(rule_spec["rate_or_discount"], "Discount Amount")
		self.assertEqual(rule_spec["discount_amount"], 10.0)
		self.assertEqual(rule_spec["apply_discount_on"], "Grand Total")
		self.assertEqual(rule_spec["valid_from"], cr.COUPON_RULE_VALID_FROM)  # Wave owns validity, no ERP date gate
		self.assertEqual(rule_spec["currency"], "KES")
		self.assertEqual(rule_spec["selling"], 1)
		self.assertEqual(rule_spec["coupon_code_based"], 1)
		# Coupon Code: links the rule, high use cap so Wave owns validity.
		self.assertEqual(coupon_spec["coupon_code"], "HYPA10")
		self.assertEqual(coupon_spec["coupon_type"], "Promotional")
		self.assertEqual(coupon_spec["maximum_use"], cr.COUPON_MAX_USE)
		self.assertEqual(coupon_spec["pricing_rule"], rule_spec["title"])

	def test_net_total_config_is_respected(self):
		specs = []

		def fake_get_doc(spec):
			doc = MagicMock()
			doc.name = spec.get("coupon_name") or spec.get("title")
			doc.insert.return_value = doc
			specs.append(spec)
			return doc

		with (
			patch.object(frappe.db, "get_value", return_value=None),
			patch.object(frappe, "get_doc", side_effect=fake_get_doc),
			patch.object(cr, "log_step"),
		):
			cr.resolve_coupon("HYPA10", 10.0, _settings(apply_on="Net Total"), "corr-1")
		self.assertEqual(specs[0]["apply_discount_on"], "Net Total")

	def test_creation_failure_raises_resolution_error(self):
		with (
			patch.object(frappe.db, "get_value", return_value=None),
			patch.object(frappe, "get_doc", side_effect=Exception("validator said no")),
			patch.object(frappe, "log_error"),
			patch.object(cr, "log_step"),
		):
			with self.assertRaises(WaveResolutionError):
				cr.resolve_coupon("HYPA10", 10.0, _settings(), "corr-1")

	def test_missing_default_currency_raises(self):
		with (
			patch.object(frappe.db, "get_value", return_value=None),
			patch.object(cr, "log_step"),
		):
			with self.assertRaises(WaveResolutionError):
				cr.resolve_coupon("HYPA10", 10.0, _settings(currency=""), "corr-1")


class TestRealign(FrappeTestCase):
	def _existing(self):
		return {"name": "HYPA10", "pricing_rule": "PR-HYPA10"}

	def _run(self, current_rule, amount, apply_on="Grand Total"):
		def fake_get_value(doctype, name_or_filters, fields=None, **k):
			return self._existing() if doctype == "Coupon Code" else current_rule

		mock_set = MagicMock()
		with (
			patch.object(frappe.db, "get_value", side_effect=fake_get_value),
			patch.object(frappe.db, "set_value", mock_set),
			patch.object(cr, "log_step"),
		):
			name = cr.resolve_coupon("HYPA10", amount, _settings(apply_on=apply_on), "corr-1")
		return name, mock_set

	def test_existing_correct_amount_no_write(self):
		current = {"rate_or_discount": "Discount Amount", "discount_amount": 10.0, "apply_discount_on": "Grand Total"}
		name, mock_set = self._run(current, 10.0)
		self.assertEqual(name, "HYPA10")
		mock_set.assert_not_called()

	def test_existing_drifted_amount_realigned_to_wave(self):
		current = {"rate_or_discount": "Discount Amount", "discount_amount": 5.0, "apply_discount_on": "Grand Total"}
		name, mock_set = self._run(current, 10.0)
		mock_set.assert_called_once()
		_, rule, changes = mock_set.call_args.args
		self.assertEqual(rule, "PR-HYPA10")
		self.assertEqual(changes["discount_amount"], 10.0)

	def test_existing_percentage_rule_is_a_mismatch(self):
		current = {"rate_or_discount": "Discount Percentage", "discount_amount": 0.0, "apply_discount_on": "Grand Total"}
		with self.assertRaises(WaveResolutionError):
			self._run(current, 10.0)


class TestGuards(FrappeTestCase):
	def test_empty_code_raises(self):
		with self.assertRaises(WaveResolutionError):
			cr.resolve_coupon("  ", 10.0, _settings(), "corr-1")

	def test_non_positive_amount_raises(self):
		with self.assertRaises(WaveResolutionError):
			cr.resolve_coupon("HYPA10", 0.0, _settings(), "corr-1")
