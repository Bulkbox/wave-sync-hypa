"""Unit tests for resolvers.contact_resolver primary-contact promotion.

The Contact insert/save and the Customer reads/writes are patched at the
frappe boundary so the orchestration (is_primary_contact + promote-when-empty
+ card sync) is pinned without touching the DB.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.resolvers import contact_resolver as cr

PAYLOAD = {
	"_id": "wave-cust-1",
	"firstName": "Ada",
	"lastName": "Lovelace",
	"email": "ada@example.com",
	"mobilePhone": "+254700000000",
}


def _fake_contact(name: str = "CONTACT-NEW", customer: str = "CUST-1") -> MagicMock:
	doc = MagicMock(name="Contact")
	doc.name = name
	doc.mobile_no = "+254700000000"
	doc.email_id = "ada@example.com"
	doc.links = [SimpleNamespace(link_doctype="Customer", link_name=customer)]
	return doc


class TestCreateContactPrimary(FrappeTestCase):
	def test_create_flags_primary_and_promotes_when_customer_has_none(self):
		captured = {}
		contact = _fake_contact()

		def fake_get_doc(spec):
			captured["spec"] = spec
			return contact

		# No existing contact; customer has no primary yet -> both None.
		with (
			patch.object(frappe.db, "get_value", return_value=None),
			patch.object(frappe, "get_doc", side_effect=fake_get_doc),
			patch.object(frappe.db, "set_value") as mock_set,
		):
			name = cr.upsert_contact("CUST-1", PAYLOAD)

		self.assertEqual(name, "CONTACT-NEW")
		self.assertEqual(captured["spec"]["is_primary_contact"], 1)
		mock_set.assert_called_once()
		dt, customer, values = mock_set.call_args.args
		self.assertEqual((dt, customer), ("Customer", "CUST-1"))
		self.assertEqual(values["customer_primary_contact"], "CONTACT-NEW")
		self.assertEqual(values["mobile_no"], "+254700000000")
		self.assertEqual(values["email_id"], "ada@example.com")

	def test_create_does_not_overwrite_an_existing_primary(self):
		def fake_get_value(doctype, name, fieldname=None, *a, **k):
			# Customer already has an operator-set primary; no existing wave contact.
			return "CONTACT-OPERATOR" if doctype == "Customer" else None

		with (
			patch.object(frappe.db, "get_value", side_effect=fake_get_value),
			patch.object(frappe, "get_doc", return_value=_fake_contact()),
			patch.object(frappe.db, "set_value") as mock_set,
		):
			cr.upsert_contact("CUST-1", PAYLOAD)
		mock_set.assert_not_called()


class TestUpdateContactCardSync(FrappeTestCase):
	def test_update_syncs_card_when_this_contact_is_primary(self):
		def fake_get_value(doctype, name, fieldname=None, *a, **k):
			# _find_contact -> existing; and the customer's primary IS this contact.
			return "CONTACT-1"

		with (
			patch.object(frappe.db, "get_value", side_effect=fake_get_value),
			patch.object(frappe, "get_doc", return_value=_fake_contact(name="CONTACT-1")),
			patch.object(frappe.db, "set_value") as mock_set,
		):
			cr.upsert_contact("CUST-1", PAYLOAD)
		mock_set.assert_called_once()
		_, _, values = mock_set.call_args.args
		# Card fields only — the primary link is left untouched on update.
		self.assertEqual(set(values), {"mobile_no", "email_id"})

	def test_update_no_sync_when_a_different_contact_is_primary(self):
		def fake_get_value(doctype, name, fieldname=None, *a, **k):
			return "CONTACT-OTHER" if doctype == "Customer" else "CONTACT-1"

		with (
			patch.object(frappe.db, "get_value", side_effect=fake_get_value),
			patch.object(frappe, "get_doc", return_value=_fake_contact(name="CONTACT-1")),
			patch.object(frappe.db, "set_value") as mock_set,
		):
			cr.upsert_contact("CUST-1", PAYLOAD)
		mock_set.assert_not_called()
