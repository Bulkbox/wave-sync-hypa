"""End-to-end tests for the CUSTOMER.UPDATE handler and its resolvers."""

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers.customer import handle
from wave_sync_hypa.wave_sync_hypa.resolvers.address_resolver import append_if_new
from wave_sync_hypa.wave_sync_hypa.resolvers.customer_resolver import (
	find_customer_by_wave_id,
	find_or_create_customer,
)


class TestCustomerHandler(FrappeTestCase):
	"""Drive handle() with realistic Wave payloads and assert ERP state + logs."""

	def setUp(self):
		"""Seed a unique wave_customer_id and make sure no stale ERP rows match it."""
		self.correlation_id = frappe.generate_hash(length=16)
		self.wave_customer_id = frappe.generate_hash(length=12)
		self.wave_address_id = frappe.generate_hash(length=12)
		self._ensure_walk_in_customer()
		self._cleanup_matching_rows()

	def tearDown(self):
		"""Remove anything this test wrote so the suite stays isolated."""
		self._cleanup_matching_rows()
		for name in frappe.get_all(
			"Wave Sync Log", filters={"correlation_id": self.correlation_id}, pluck="name"
		):
			frappe.delete_doc("Wave Sync Log", name, ignore_permissions=True, delete_permanently=True)

	def _ensure_walk_in_customer(self) -> None:
		"""Point Wave Settings at a guaranteed-to-exist Customer for guest-payload tests."""
		existing = frappe.db.get_single_value("Wave Settings", "walk_in_customer")
		if existing and frappe.db.exists("Customer", existing):
			self.walk_in = existing
			return
		self.walk_in = frappe.db.get_value("Customer", {"customer_type": "Individual"}, "name")
		if not self.walk_in:
			self.walk_in = self._create_minimal_customer("Wave Walk-in Test")
		frappe.db.set_value(
			"Wave Settings", "Wave Settings", "walk_in_customer", self.walk_in, update_modified=False
		)
		frappe.db.commit()
		frappe.clear_document_cache("Wave Settings", "Wave Settings")

	def _create_minimal_customer(self, name: str) -> str:
		"""Create the fallback walk-in customer once per test environment."""
		doc = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": name,
				"customer_type": "Individual",
				"customer_group": frappe.db.get_value(
					"Customer Group", {"is_group": 0}, "name"
				) or "All Customer Groups",
				"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name") or "All Territories",
			}
		)
		doc.insert(ignore_permissions=True)
		return doc.name

	def _cleanup_matching_rows(self) -> None:
		"""Delete ERP rows this test's Wave ids would have produced, from leaves to root."""
		for addr in frappe.get_all(
			"Address", filters={"wave_address_id": self.wave_address_id}, pluck="name"
		):
			frappe.delete_doc("Address", addr, ignore_permissions=True, delete_permanently=True)
		for contact in frappe.get_all(
			"Contact", filters={"wave_contact_id": self.wave_customer_id}, pluck="name"
		):
			frappe.delete_doc("Contact", contact, ignore_permissions=True, delete_permanently=True)
		for cust in frappe.get_all(
			"Customer", filters={"wave_customer_id": self.wave_customer_id}, pluck="name"
		):
			frappe.delete_doc("Customer", cust, ignore_permissions=True, delete_permanently=True)

	def _payload(self, **overrides) -> dict:
		"""Build a minimal CUSTOMER.UPDATE payload for tests; overrides merge on top."""
		base = {
			"_id": self.wave_customer_id,
			"updatedAt": "1776753292987",
			"email": "wave.test@example.com",
			"firstName": "Sotirios",
			"lastName": "Meintanis",
			"mobilePhone": "+306977158099",
			"integratorId": "Sotirios Meintanis",
			"isGuest": False,
			"addresses": [],
		}
		base.update(overrides)
		return base

	def _address(self, **overrides) -> dict:
		"""Build a minimal Wave address dict."""
		base = {
			"_id": self.wave_address_id,
			"type": "home",
			"city": "Nairobi",
			"street": "Muthithi Road",
			"streetNo": "0010",
			"postalCode": "00100",
			"contactPhone": "6977158099",
			"timeZone": "Africa/Nairobi",
		}
		base.update(overrides)
		return base

	def test_creates_customer_and_contact_on_first_call(self):
		"""First CUSTOMER.UPDATE persists Customer (with wave_customer_id) and a linked Contact."""
		handle(self._payload(), self.correlation_id)

		customer_name = find_customer_by_wave_id(self.wave_customer_id)
		self.assertIsNotNone(customer_name)
		customer = frappe.get_doc("Customer", customer_name)
		self.assertEqual(customer.customer_name, "Sotirios Meintanis")
		self.assertEqual(customer.wave_integrator_id, "Sotirios Meintanis")
		self.assertEqual(int(customer.is_wave_customer), 1)

		contact_name = frappe.db.get_value("Contact", {"wave_contact_id": self.wave_customer_id}, "name")
		self.assertIsNotNone(contact_name)
		contact = frappe.get_doc("Contact", contact_name)
		self.assertEqual(contact.first_name, "Sotirios")
		self.assertEqual(len(contact.email_ids), 1)
		self.assertEqual(contact.email_ids[0].email_id, "wave.test@example.com")

	def test_updates_existing_customer_without_duplicating(self):
		"""Second CUSTOMER.UPDATE with the same wave_customer_id updates in place, no new row."""
		handle(self._payload(), self.correlation_id)
		handle(self._payload(lastName="Meintaniss"), self.correlation_id)
		matches = frappe.get_all("Customer", filters={"wave_customer_id": self.wave_customer_id})
		self.assertEqual(len(matches), 1)
		customer = frappe.get_doc("Customer", matches[0].name)
		self.assertEqual(customer.customer_name, "Sotirios Meintaniss")

	def test_guest_payloads_route_to_walk_in_customer(self):
		"""isGuest=true returns the walk-in customer name without creating a new Customer."""
		customer_name, created = find_or_create_customer(self._payload(isGuest=True))
		self.assertEqual(customer_name, self.walk_in)
		self.assertFalse(created)
		# Also: running the full handler with a guest payload does not create a Wave-linked Customer.
		handle(self._payload(isGuest=True), self.correlation_id)
		self.assertFalse(find_customer_by_wave_id(self.wave_customer_id))

	def test_new_address_is_appended_without_mutating_old(self):
		"""Sending a new address _id after the first run creates a new Address; original untouched."""
		handle(self._payload(addresses=[self._address()]), self.correlation_id)
		first = frappe.db.get_value("Address", {"wave_address_id": self.wave_address_id}, "name")
		self.assertIsNotNone(first)
		first_city = frappe.db.get_value("Address", first, "city")

		new_wave_address_id = frappe.generate_hash(length=12)
		handle(
			self._payload(
				addresses=[self._address(_id=new_wave_address_id, city="Mombasa", streetNo="42")]
			),
			self.correlation_id,
		)

		# Original Address must be unchanged.
		self.assertEqual(frappe.db.get_value("Address", first, "city"), first_city)
		# New Address must exist with the new city.
		new_name = frappe.db.get_value("Address", {"wave_address_id": new_wave_address_id}, "name")
		self.assertIsNotNone(new_name)
		self.assertEqual(frappe.db.get_value("Address", new_name, "city"), "Mombasa")

		# Cleanup the second address so the test tearDown can find and remove it.
		frappe.delete_doc("Address", new_name, ignore_permissions=True, delete_permanently=True)

	def test_duplicate_address_is_not_re_inserted(self):
		"""Same wave_address_id received twice returns the existing Address, no second row."""
		handle(self._payload(addresses=[self._address()]), self.correlation_id)
		handle(self._payload(addresses=[self._address()]), self.correlation_id)
		matches = frappe.get_all("Address", filters={"wave_address_id": self.wave_address_id})
		self.assertEqual(len(matches), 1)

	def test_append_if_new_returns_created_flag(self):
		"""Direct resolver-level check: append_if_new signals whether it inserted."""
		# Make sure the customer exists first so the Address can link to it.
		handle(self._payload(), self.correlation_id)
		customer = find_customer_by_wave_id(self.wave_customer_id)
		_, created_first = append_if_new(customer, self._address())
		_, created_second = append_if_new(customer, self._address())
		self.assertTrue(created_first)
		self.assertFalse(created_second)

	def test_handler_writes_resolved_customer_log_row(self):
		"""After handle() runs, Wave Sync Log contains a Resolved Customer row tied to the customer."""
		handle(self._payload(), self.correlation_id)
		rows = frappe.get_all(
			"Wave Sync Log",
			filters={"correlation_id": self.correlation_id, "step": "Resolved Customer"},
			fields=["linked_doctype", "linked_docname"],
		)
		self.assertGreaterEqual(len(rows), 1)
		customer_rows = [r for r in rows if r.linked_doctype == "Customer"]
		self.assertEqual(len(customer_rows), 1)
		self.assertEqual(
			customer_rows[0].linked_docname, find_customer_by_wave_id(self.wave_customer_id)
		)
