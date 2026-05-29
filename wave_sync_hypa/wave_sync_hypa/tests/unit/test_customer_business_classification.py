"""Unit tests for the B2B classification helpers in resolvers.customer_resolver.

Pure: every test patches frappe.db lookups so we can exercise the branching
logic (customerType, companyName, businessType, fiscalId/taxId) without
touching a Customer Group, Customer, or Address record on disk.

The integration shape (create + update flow end-to-end through handle()) is
already covered by test_customer_handler.py; here we focus on the unit-level
decision matrix.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.resolvers import customer_resolver as cr


def _b2b_payload(**overrides) -> dict:
	"""Realistic B2B CUSTOMER.UPDATE payload mirrored from Wave's new shape."""
	base = {
		"_id": "69fc71e2610266ae6e91b780",
		"email": "rashid@hypa.example",
		"firstName": "Rashid",
		"lastName": "Salim",
		"mobilePhone": "+254701955812",
		"customerType": "b2b",
		"companyName": "Hypa 2",
		"businessAddress": "Unga House, Muthithi Road",
		"city": "Nairobi",
		"businessType": "restaurant/cafe/hotel",
		"fiscalId": "100100",
		"isGuest": False,
	}
	base.update(overrides)
	return base


def _b2c_payload(**overrides) -> dict:
	"""Legacy B2C payload — what we had before this feature."""
	base = {
		"_id": "69af39b2aee2cc5370a00c36",
		"email": "indi@example.com",
		"firstName": "Indi",
		"lastName": "Vidual",
		"mobilePhone": "+254700000000",
		"isGuest": False,
	}
	base.update(overrides)
	return base


class TestResolveCustomerType(FrappeTestCase):
	def test_b2b_maps_to_company(self):
		self.assertEqual(cr._resolve_customer_type(_b2b_payload()), "Company")

	def test_b2c_maps_to_individual(self):
		self.assertEqual(cr._resolve_customer_type(_b2c_payload(customerType="b2c")), "Individual")

	def test_missing_customerType_returns_none_for_update_safety(self):
		"""Legacy b2c payloads omit customerType entirely — return None so
		apply_customer_updates leaves the existing field alone."""
		self.assertIsNone(cr._resolve_customer_type(_b2c_payload()))

	def test_uppercase_or_padded_value_is_normalised(self):
		self.assertEqual(cr._resolve_customer_type({"customerType": "  B2B  "}), "Company")


class TestResolveCustomerName(FrappeTestCase):
	def test_b2b_prefers_company_name(self):
		self.assertEqual(cr._resolve_customer_name(_b2b_payload()), "Hypa 2")

	def test_b2b_falls_back_to_full_name_when_company_blank(self):
		payload = _b2b_payload(companyName="")
		self.assertEqual(cr._resolve_customer_name(payload), "Rashid Salim")

	def test_b2c_uses_full_name(self):
		self.assertEqual(cr._resolve_customer_name(_b2c_payload()), "Indi Vidual")

	def test_missing_customerType_uses_full_name(self):
		"""No customerType in payload (legacy) -> full name, not companyName."""
		payload = _b2c_payload(companyName="Should Be Ignored")
		self.assertEqual(cr._resolve_customer_name(payload), "Indi Vidual")


class TestResolveTaxId(FrappeTestCase):
	def test_fiscal_id_today(self):
		self.assertEqual(cr._resolve_tax_id(_b2b_payload()), "100100")

	def test_tax_id_in_future(self):
		"""When Wave migrates fiscalId -> taxId, the resolver picks it up automatically."""
		payload = _b2b_payload()
		del payload["fiscalId"]
		payload["taxId"] = "P051234567B"
		self.assertEqual(cr._resolve_tax_id(payload), "P051234567B")

	def test_fiscal_id_wins_when_both_present(self):
		"""Transition period: prefer fiscalId so old Wave deployments don't break."""
		payload = _b2b_payload(taxId="ignored")
		self.assertEqual(cr._resolve_tax_id(payload), "100100")

	def test_neither_present_returns_none(self):
		self.assertIsNone(cr._resolve_tax_id(_b2c_payload()))

	def test_whitespace_only_treated_as_missing(self):
		self.assertIsNone(cr._resolve_tax_id({"fiscalId": "   "}))


class TestResolveCustomerGroup(FrappeTestCase):
	def test_b2b_uses_business_type_verbatim_when_group_exists(self):
		with (
			patch.object(frappe.db, "exists", return_value=True),
			patch.object(frappe.db, "get_single_value", return_value="Commercial"),
		):
			self.assertEqual(
				cr._resolve_customer_group(_b2b_payload()),
				"restaurant/cafe/hotel",
			)

	def test_b2b_falls_back_to_default_when_group_missing_and_logs(self):
		with (
			patch.object(frappe.db, "exists", return_value=False),
			patch.object(frappe.db, "get_single_value", return_value="Commercial"),
			patch.object(frappe, "log_error") as mock_log,
		):
			self.assertEqual(cr._resolve_customer_group(_b2b_payload()), "Commercial")
		# The missing-group fallback writes to the Frappe Error Log for triage.
		self.assertEqual(mock_log.call_count, 1)
		(_, kwargs) = mock_log.call_args
		# Title + message both mention the businessType so the row is findable.
		self.assertIn("restaurant/cafe/hotel", kwargs.get("message", ""))

	def test_b2c_uses_default_customer_group(self):
		with patch.object(frappe.db, "get_single_value", return_value="Individual"):
			self.assertEqual(cr._resolve_customer_group(_b2c_payload()), "Individual")

	def test_b2b_with_blank_business_type_uses_default(self):
		payload = _b2b_payload(businessType="")
		with patch.object(frappe.db, "get_single_value", return_value="Commercial"):
			self.assertEqual(cr._resolve_customer_group(payload), "Commercial")


class TestAppendBusinessAddressIfPresent(FrappeTestCase):
	"""Idempotency + label semantics for the synthesised business address."""

	def test_b2c_payload_no_op(self):
		"""B2C customers never get a synthesised business address."""
		with patch.object(cr, "append_if_new") as mock_append:
			result = cr.append_business_address_if_present("CUST-X", _b2c_payload())
		self.assertIsNone(result)
		mock_append.assert_not_called()

	def test_b2b_without_business_address_no_op(self):
		"""B2B payload but no businessAddress sent -> no-op."""
		payload = _b2b_payload(businessAddress="")
		with patch.object(cr, "append_if_new") as mock_append:
			result = cr.append_business_address_if_present("CUST-X", payload)
		self.assertIsNone(result)
		mock_append.assert_not_called()

	def test_b2b_synthesises_address_with_deterministic_id(self):
		"""The synth id is deterministic so re-sends de-dup via append_if_new."""
		with (
			patch.object(cr, "append_if_new", return_value=("ADDR-NEW", True)) as mock_append,
			patch.object(frappe.db, "set_value") as mock_set,
		):
			cr.append_business_address_if_present("Hypa 2", _b2b_payload())
		mock_append.assert_called_once()
		customer_arg, synth = mock_append.call_args.args
		self.assertEqual(customer_arg, "Hypa 2")
		# Deterministic id: same payload twice would map to the same Address.
		self.assertEqual(synth["_id"], "business:69fc71e2610266ae6e91b780")
		# Type maps to "Office" via the existing address_resolver type table.
		self.assertEqual(synth["type"], "headquarters")
		self.assertEqual(synth["street"], "Unga House, Muthithi Road")
		self.assertEqual(synth["city"], "Nairobi")
		# Newly-created addresses get a friendly title.
		mock_set.assert_called_once()
		self.assertEqual(mock_set.call_args.args[2], "address_title")
		self.assertEqual(mock_set.call_args.args[3], "Hypa 2 - Business Address")

	def test_existing_business_address_no_rename(self):
		"""When append_if_new finds an existing Address, we don't rename it."""
		with (
			patch.object(cr, "append_if_new", return_value=("ADDR-EXISTING", False)),
			patch.object(frappe.db, "set_value") as mock_set,
		):
			cr.append_business_address_if_present("Hypa 2", _b2b_payload())
		mock_set.assert_not_called()
