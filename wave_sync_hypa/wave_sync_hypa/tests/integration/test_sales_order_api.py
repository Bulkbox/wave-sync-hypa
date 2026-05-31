"""Unit tests for api.sales_order: clear_manual_review_flag + push_to_wave."""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.api import sales_order as so_api
from wave_sync_hypa.wave_sync_hypa.api.sales_order import clear_manual_review_flag, push_to_wave


class TestClearManualReviewFlag(FrappeTestCase):
	"""Seed an SO with the flag on, call the endpoint, assert flag off + Comment added."""

	def setUp(self):
		"""Create a draft Sales Order with wave_manual_review_required=1 for this test to act on."""
		self.sales_order = self._create_minimal_sales_order()
		frappe.db.set_value(
			"Sales Order",
			self.sales_order,
			"wave_manual_review_required",
			1,
			update_modified=False,
		)
		frappe.db.commit()

	def tearDown(self):
		"""Delete Comments and the SO this test created; absorb ERPNext lock races."""
		frappe.db.rollback()
		for name in frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Sales Order", "reference_name": self.sales_order},
			pluck="name",
		):
			self._safe_delete("Comment", name)
		self._safe_delete("Sales Order", self.sales_order)

	def _safe_delete(self, doctype: str, name: str) -> None:
		"""Delete a doc, committing between removals so locks don't cascade."""
		try:
			frappe.delete_doc(doctype, name, ignore_permissions=True, delete_permanently=True)
			frappe.db.commit()
		except frappe.QueryTimeoutError:
			frappe.db.rollback()

	def _create_minimal_sales_order(self) -> str:
		"""Insert a minimal draft Sales Order linked to the configured walk-in customer."""
		customer = frappe.db.get_single_value("Wave Settings", "walk_in_customer")
		if not customer:
			self.skipTest("Wave Settings.walk_in_customer is not configured on this site.")
		company = frappe.db.get_single_value("Wave Settings", "default_company") or frappe.db.get_value(
			"Company", {"is_group": 0}, "name"
		)
		price_list = frappe.db.get_value("Price List", {"enabled": 1, "selling": 1}, "name")
		item_code = frappe.db.sql(
			"""SELECT ip.item_code FROM `tabItem Price` ip
			JOIN `tabItem` it ON it.name=ip.item_code
			WHERE ip.price_list=%(pl)s AND it.disabled=0 LIMIT 1""",
			{"pl": price_list},
		)
		if not item_code:
			self.skipTest(f"No priced Items in {price_list!r} available for SO creation.")
		doc = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": customer,
				"company": company,
				"selling_price_list": price_list,
				"currency": frappe.db.get_value("Company", company, "default_currency") or "KES",
				"transaction_date": frappe.utils.getdate(),
				"delivery_date": frappe.utils.add_days(frappe.utils.getdate(), 1),
				"order_type": "Sales",
				"items": [{"item_code": item_code[0][0], "qty": 1}],
			}
		)
		doc.flags.ignore_mandatory = True
		doc.insert(ignore_permissions=True)
		return doc.name

	def test_endpoint_clears_the_flag(self):
		"""clear_manual_review_flag sets wave_manual_review_required to 0 on the target SO."""
		self.assertEqual(
			int(frappe.db.get_value("Sales Order", self.sales_order, "wave_manual_review_required") or 0),
			1,
			"setUp must seed the flag as 1.",
		)
		result = clear_manual_review_flag(self.sales_order)
		self.assertEqual(result, {"ok": True, "sales_order": self.sales_order})
		self.assertEqual(
			int(frappe.db.get_value("Sales Order", self.sales_order, "wave_manual_review_required") or 0),
			0,
		)

	def test_endpoint_appends_audit_comment(self):
		"""Clearing the flag leaves a Comment naming the user who did it."""
		clear_manual_review_flag(self.sales_order)
		comments = frappe.get_all(
			"Comment",
			filters={
				"reference_doctype": "Sales Order",
				"reference_name": self.sales_order,
				"comment_type": "Comment",
			},
			fields=["content"],
		)
		# At least one Comment must mention that the flag was cleared by the current user.
		cleared_comments = [c for c in comments if "cleared by" in (c.content or "")]
		self.assertEqual(len(cleared_comments), 1)
		self.assertIn(frappe.session.user, cleared_comments[0].content)

	def test_endpoint_refuses_user_without_write_permission(self):
		"""A user who cannot write Sales Order must not be able to clear the flag."""
		# Guest has no Sales Order write permission by default.
		original_user = frappe.session.user
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				clear_manual_review_flag(self.sales_order)
		finally:
			frappe.set_user(original_user)
		# The flag must still be 1 because the call was refused.
		self.assertEqual(
			int(frappe.db.get_value("Sales Order", self.sales_order, "wave_manual_review_required") or 0),
			1,
		)


class TestPushToWave(FrappeTestCase):
	"""Unit tests for the push_to_wave whitelisted endpoint (delegates to wave_order_creator)."""

	def test_delegates_to_creator_and_forwards_success_result(self):
		"""Happy path: API returns the creator's result + adds correlation_id."""
		fake_doc = MagicMock(name="SalesOrderDoc")
		fake_doc.check_permission.return_value = None
		creator_result = {"ok": True, "wave_order_id": "wave-x", "wave_friendly_id": "10001"}
		with (
			patch.object(frappe, "get_doc", return_value=fake_doc),
			patch.object(so_api.wave_order_creator, "push_so_to_wave", return_value=dict(creator_result)) as mock_push,
			patch.object(so_api, "new_correlation_id", return_value="corr-happy"),
			patch.object(frappe.db, "commit"),
		):
			result = push_to_wave("SAL-ORD-X")
		fake_doc.check_permission.assert_called_once_with("write")
		mock_push.assert_called_once_with("SAL-ORD-X", "corr-happy")
		self.assertEqual(result["ok"], True)
		self.assertEqual(result["wave_order_id"], "wave-x")
		self.assertEqual(result["wave_friendly_id"], "10001")
		self.assertEqual(result["correlation_id"], "corr-happy")

	def test_delegates_to_creator_and_forwards_failure_result(self):
		"""Failure path: API returns the creator's {ok: False, reason} + adds correlation_id."""
		fake_doc = MagicMock(name="SalesOrderDoc")
		fake_doc.check_permission.return_value = None
		creator_result = {"ok": False, "reason": "missing wave_product_id for [JTD099]"}
		with (
			patch.object(frappe, "get_doc", return_value=fake_doc),
			patch.object(so_api.wave_order_creator, "push_so_to_wave", return_value=dict(creator_result)),
			patch.object(so_api, "new_correlation_id", return_value="corr-fail"),
			patch.object(frappe.db, "commit"),
		):
			result = push_to_wave("SAL-ORD-Y")
		self.assertFalse(result["ok"])
		self.assertIn("JTD099", result["reason"])
		self.assertEqual(result["correlation_id"], "corr-fail")

	def test_check_permission_runs_before_creator(self):
		"""PermissionError from check_permission must short-circuit before any push happens."""
		fake_doc = MagicMock(name="SalesOrderDoc")
		fake_doc.check_permission.side_effect = frappe.PermissionError("no write")
		with (
			patch.object(frappe, "get_doc", return_value=fake_doc),
			patch.object(so_api.wave_order_creator, "push_so_to_wave") as mock_push,
			patch.object(so_api, "new_correlation_id"),
			patch.object(frappe.db, "commit"),
		):
			with self.assertRaises(frappe.PermissionError):
				push_to_wave("SAL-ORD-Z")
		mock_push.assert_not_called()
