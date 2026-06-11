"""Unit tests for side effects when a new Wave customer is created.

`_create_customer_from_wave` now does two extra things, create-only:
  1. creates + links the primary Contact (so order-originated customers are
     not left contactless, mirroring how addresses are handled), and
  2. seeds the credit limit from Wave Settings (Default Company + configurable
     amount).

frappe.get_doc, the Wave Settings reads (`_default`), and `upsert_contact` are
patched at the boundary so the test pins the orchestration, not the DB.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.resolvers import customer_resolver as cr

PAYLOAD = {
	"_id": "wave-cust-1",
	"firstName": "Ada",
	"lastName": "Lovelace",
	"email": "ada@example.com",
	"mobilePhone": "+254700000000",
}


def _defaults(credit_limit=50000, company="Acme Ltd"):
	return {
		"default_customer_credit_limit": credit_limit,
		"default_company": company,
		"default_territory": "Kenya",
		"default_customer_group": "Consumer",
	}


def _run_create(defaults):
	"""Invoke the real _create_customer_from_wave with DB boundaries mocked."""
	captured = {}

	def fake_get_doc(spec):
		captured["spec"] = spec
		doc = MagicMock(name="Customer")
		doc.name = "CUST-NEW"
		doc.flags = SimpleNamespace()
		return doc

	with (
		patch.object(cr, "_default", side_effect=lambda key: defaults.get(key)),
		patch.object(frappe.db, "get_value", return_value="All Customer Groups"),
		patch.object(frappe, "get_doc", side_effect=fake_get_doc),
		patch.object(cr, "upsert_contact") as mock_contact,
	):
		name = cr._create_customer_from_wave(PAYLOAD)
	return name, captured["spec"], mock_contact


class TestCustomerCreateSideEffects(FrappeTestCase):
	def test_contact_created_and_linked_on_creation(self):
		name, _spec, mock_contact = _run_create(_defaults())
		self.assertEqual(name, "CUST-NEW")
		mock_contact.assert_called_once_with("CUST-NEW", PAYLOAD)

	def test_credit_limit_seeded_from_settings_for_default_company(self):
		_name, spec, _c = _run_create(_defaults(credit_limit=50000, company="Acme Ltd"))
		self.assertEqual(spec["credit_limits"], [{"company": "Acme Ltd", "credit_limit": 50000}])

	def test_credit_limit_omitted_when_amount_unset(self):
		_name, spec, _c = _run_create(_defaults(credit_limit=0))
		self.assertEqual(spec["credit_limits"], [])

	def test_credit_limit_omitted_when_no_default_company(self):
		_name, spec, _c = _run_create(_defaults(company=None))
		self.assertEqual(spec["credit_limits"], [])
