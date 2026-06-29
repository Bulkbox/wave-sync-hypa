"""Integration test for the prepaid Payment Entry engine (issue #193).

Exercises the real-doc path the unit tests mock: ensure_draft_pe_for_order
inserts a genuine UNALLOCATED Receive Payment Entry (references=[], paid_from
auto-derived) for a submitted prepaid Sales Order, and is idempotent. Skips
when the site lacks the needed fixtures (walk-in customer, a priced sellable
item, an MPESA Mode of Payment account).
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.services import prepaid_pe_creator as pe_creator

TXN = "WAVE-IT-PREPAID-PE-0001"
WOID = "wave-it-prepaid-0001"


class TestPrepaidDraftPaymentEntry(FrappeTestCase):
	def setUp(self):
		self.so = self._create_submitted_prepaid_so()

	def tearDown(self):
		frappe.db.rollback()
		for pe in frappe.get_all("Payment Entry", filters={"reference_no": TXN}, pluck="name"):
			self._safe_delete("Payment Entry", pe)
		if getattr(self, "so", None):
			try:
				doc = frappe.get_doc("Sales Order", self.so)
				if doc.docstatus == 1:
					doc.cancel()
					frappe.db.commit()
			except Exception:
				frappe.db.rollback()
			self._safe_delete("Sales Order", self.so)

	def _safe_delete(self, doctype, name):
		try:
			frappe.delete_doc(doctype, name, ignore_permissions=True, delete_permanently=True)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()

	def _create_submitted_prepaid_so(self) -> str:
		customer = frappe.db.get_single_value("Wave Settings", "walk_in_customer")
		if not customer:
			self.skipTest("Wave Settings.walk_in_customer is not configured.")
		company = frappe.db.get_single_value("Wave Settings", "default_company") or frappe.db.get_value(
			"Company", {"is_group": 0}, "name"
		)
		if not frappe.db.get_value("Mode of Payment Account", {"parent": "MPESA", "company": company}, "default_account"):
			self.skipTest("MPESA Mode of Payment has no account on this company.")
		# Resolve a sellable item together with the enabled selling price list it is
		# actually priced in (picking any enabled selling list may have no prices).
		row = frappe.db.sql(
			"""SELECT ip.item_code, ip.price_list FROM `tabItem Price` ip
			JOIN `tabItem` it ON it.name=ip.item_code
			JOIN `tabPrice List` pl ON pl.name=ip.price_list
			WHERE it.disabled=0 AND it.is_sales_item=1 AND ip.selling=1 AND pl.enabled=1 LIMIT 1""",
			as_dict=True,
		)
		if not row:
			self.skipTest("No priced sellable Items in an enabled selling price list.")
		item_code, price_list = row[0].item_code, row[0].price_list
		doc = frappe.get_doc({
			"doctype": "Sales Order",
			"customer": customer,
			"company": company,
			"selling_price_list": price_list,
			"currency": frappe.db.get_value("Company", company, "default_currency") or "KES",
			"transaction_date": frappe.utils.getdate(),
			"delivery_date": frappe.utils.add_days(frappe.utils.getdate(), 1),
			"order_type": "Sales",
			"items": [{"item_code": item_code, "qty": 1}],
		})
		doc.flags.ignore_mandatory = True
		doc.insert(ignore_permissions=True)
		try:
			doc.submit()
		except Exception as exc:
			self._safe_delete("Sales Order", doc.name)
			self.skipTest(f"Could not submit a minimal Sales Order on this site: {exc}")
		frappe.db.set_value(
			"Sales Order", doc.name,
			{
				"wave_payment_classification": "prepaid",
				"wave_ipay_transaction_code": TXN,
				"wave_ipay_paid_at": frappe.utils.now_datetime(),
				"wave_payment_type": "card",
				"wave_payment_hold": doc.grand_total,
				"wave_order_id": WOID,
				"wave_friendly_id": "ITPP0001",
			},
			update_modified=False,
		)
		frappe.db.commit()
		return doc.name

	def _build(self, correlation_id):
		settings = frappe.get_cached_doc("Wave Settings")
		# dev mappings carry no mode_of_payment; force a mapped MOP that has an account.
		with patch.object(pe_creator.payment_mapping, "mode_of_payment_for", return_value="MPESA"):
			pe_creator.ensure_draft_pe_for_order(self.so, correlation_id, settings=settings)

	def test_builds_real_unallocated_draft(self):
		self._build("it-build")
		pes = frappe.get_all(
			"Payment Entry", filters={"reference_no": TXN},
			fields=["name", "docstatus", "payment_type", "paid_from", "mode_of_payment", "wave_order_id"],
		)
		self.assertEqual(len(pes), 1)
		pe = pes[0]
		self.assertEqual(pe.docstatus, 0)              # unallocated draft, not submitted
		self.assertEqual(pe.payment_type, "Receive")
		self.assertTrue(pe.paid_from)                  # receivable auto-derived by insert()
		self.assertEqual(pe.mode_of_payment, "MPESA")
		self.assertEqual(pe.wave_order_id, WOID)
		refs = frappe.get_all("Payment Entry Reference", filters={"parent": pe.name}, pluck="name")
		self.assertEqual(refs, [])                     # references=[]

	def test_idempotent_second_call_makes_no_second_pe(self):
		self._build("it-1")
		self._build("it-2")
		self.assertEqual(len(frappe.get_all("Payment Entry", filters={"reference_no": TXN})), 1)
