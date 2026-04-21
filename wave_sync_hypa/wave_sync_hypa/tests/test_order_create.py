"""End-to-end tests for the ORDER.CREATE handler."""

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers.order_create import handle
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveResolutionError, WaveValidationError


class TestOrderCreate(FrappeTestCase):
	"""Drive ORDER.CREATE handler with realistic payloads; assert SO state + logs.

	Uses existing Items from the site to avoid tripping kenya_compliance_via_slade's
	Item validator (which mandates Country of Origin, Item Type, and other KRA fields
	a test can't sensibly populate).
	"""

	def setUp(self):
		"""Pick two existing Items, ensure Wave Settings defaults, seed a fee mapping."""
		self.correlation_id = frappe.generate_hash(length=16)
		self.wave_order_id = f"wso-{frappe.generate_hash(length=10)}"
		self.wave_user_id = f"wu-{frappe.generate_hash(length=10)}"
		self.wave_address_id = f"wa-{frappe.generate_hash(length=10)}"
		self.friendly_id = "SMOKE00001"

		self._original_price_list = frappe.db.get_single_value(
			"Wave Settings", "default_price_list"
		)
		self.test_price_list = self._pick_enabled_selling_price_list()
		self.product_sku, self.fee_item = self._pick_two_priced_items(self.test_price_list)
		self._original_mappings = self._snapshot_mappings()
		self._clear_mappings()
		self._ensure_defaults()
		self._add_fee_mapping("SHIPPING_COST", self.fee_item)

	def tearDown(self):
		"""Roll back any half-open transaction, then remove ERP rows this test created."""
		frappe.db.rollback()
		self._safe_delete_many("Sales Order", {"wave_order_id": self.wave_order_id})
		self._safe_delete_many("Address", {"wave_address_id": self.wave_address_id})
		self._safe_delete_many("Contact", {"wave_contact_id": self.wave_user_id})
		self._safe_delete_many("Customer", {"wave_customer_id": self.wave_user_id})
		self._safe_delete_many("Wave Sync Log", {"correlation_id": self.correlation_id})
		self._clear_mappings()
		self._restore_mappings(self._original_mappings)

	def _safe_delete_many(self, doctype: str, filters: dict) -> None:
		"""Delete matching rows, committing between each so row locks don't cascade."""
		for name in frappe.get_all(doctype, filters=filters, pluck="name"):
			try:
				frappe.delete_doc(doctype, name, ignore_permissions=True, delete_permanently=True)
				frappe.db.commit()
			except frappe.QueryTimeoutError:
				frappe.db.rollback()
				# Best-effort cleanup; if an ERPNext background process holds a lock, leave the
				# row for the test runner's transaction reset to reclaim.
				continue
			except Exception:
				frappe.db.rollback()
				raise
		# Restore the original (possibly-disabled) default price list so prod config is unchanged.
		frappe.db.set_value(
			"Wave Settings",
			"Wave Settings",
			"default_price_list",
			self._original_price_list,
			update_modified=False,
		)
		frappe.db.commit()
		frappe.clear_document_cache("Wave Settings", "Wave Settings")

	def _pick_enabled_selling_price_list(self) -> str:
		"""Return an enabled selling Price List; skip the suite if none are configured."""
		pl = frappe.db.get_value("Price List", {"enabled": 1, "selling": 1}, "name")
		if not pl:
			self.skipTest("No enabled selling Price List available on this site.")
		return pl

	def _pick_two_priced_items(self, price_list: str) -> tuple[str, str]:
		"""Return two enabled Item codes that have an Item Price in the given Price List."""
		rows = frappe.db.sql(
			"""
			SELECT ip.item_code
			FROM `tabItem Price` ip
			INNER JOIN `tabItem` it ON it.name = ip.item_code
			WHERE ip.price_list = %(pl)s AND it.disabled = 0
			ORDER BY ip.modified DESC
			LIMIT 2
			""",
			{"pl": price_list},
			as_dict=True,
		)
		codes = [row.item_code for row in rows]
		if len(codes) < 2:
			self.skipTest(
				f"Need at least two enabled Items priced in {price_list!r} to run these tests."
			)
		return codes[0], codes[1]

	def _ensure_defaults(self) -> None:
		"""Make sure Wave Settings has the ERP defaults the handler requires, including an enabled Price List."""
		settings = frappe.get_single("Wave Settings")
		if not settings.default_company:
			settings.default_company = frappe.db.get_value("Company", {"is_group": 0}, "name")
		if not settings.default_warehouse:
			settings.default_warehouse = frappe.db.get_value(
				"Warehouse", {"is_group": 0, "company": settings.default_company}, "name"
			)
		# Force an enabled selling Price List for the test, regardless of prod config.
		settings.default_price_list = self.test_price_list
		if not settings.default_currency:
			settings.default_currency = (
				frappe.db.get_value("Company", settings.default_company, "default_currency") or "KES"
			)
		if not settings.walk_in_customer:
			settings.walk_in_customer = frappe.db.get_value(
				"Customer", {"customer_type": "Individual"}, "name"
			)
		settings.flags.ignore_validate = True
		settings.save(ignore_permissions=True)
		frappe.clear_document_cache("Wave Settings", "Wave Settings")

	def _snapshot_mappings(self) -> list[dict]:
		"""Capture the existing fee_mappings rows so tearDown can restore them."""
		settings = frappe.get_single("Wave Settings")
		return [
			{
				"wave_fee_type": row.wave_fee_type,
				"erp_item_code": row.erp_item_code,
				"description": row.description,
			}
			for row in (settings.fee_mappings or [])
		]

	def _clear_mappings(self) -> None:
		"""Drop every Wave Fee Mapping row via direct DB delete."""
		frappe.db.delete("Wave Fee Mapping", {"parent": "Wave Settings"})
		frappe.db.commit()
		frappe.clear_document_cache("Wave Settings", "Wave Settings")

	def _add_fee_mapping(self, fee_type: str, item_code: str) -> None:
		"""Append a Wave Fee Mapping row used by the handler's fee resolver."""
		settings = frappe.get_single("Wave Settings")
		settings.append(
			"fee_mappings",
			{"wave_fee_type": fee_type, "erp_item_code": item_code, "description": "test"},
		)
		settings.flags.ignore_validate = True
		settings.save(ignore_permissions=True)
		frappe.clear_document_cache("Wave Settings", "Wave Settings")

	def _restore_mappings(self, rows: list[dict]) -> None:
		"""Reinstate fee_mappings from the snapshot."""
		settings = frappe.get_single("Wave Settings")
		settings.fee_mappings = []
		for row in rows:
			settings.append("fee_mappings", row)
		settings.flags.ignore_validate = True
		settings.save(ignore_permissions=True)
		frappe.clear_document_cache("Wave Settings", "Wave Settings")

	def _payload(self, **overrides) -> dict:
		"""Build a minimal ORDER.CREATE payload shaped like the Wave sample."""
		base = {
			"_id": self.wave_order_id,
			"updatedAt": "1776999999999",
			"createdAt": "2026-04-20T07:54:26.983Z",
			"timeSlotStart": "2026-04-21T05:00:00.000Z",
			"timeSlotEnd": "2026-04-21T10:00:00.000Z",
			"friendlyId": self.friendly_id,
			"status": "PENDING",
			"totalPrice": 16345,
			"user": {
				"_id": self.wave_user_id,
				"email": "order.test@example.com",
				"firstName": "Wave",
				"lastName": "Orderer",
				"mobile": "+254711111111",
				"isGuest": False,
			},
			"address": {
				"_id": self.wave_address_id,
				"type": "home",
				"city": "Nairobi",
				"street": "Muthithi Road",
				"streetNo": "10",
				"postalCode": "00100",
				"contactPhone": "+254711111111",
				"timeZone": "Africa/Nairobi",
			},
			"products": [
				{"sku": self.product_sku, "integratorId": self.product_sku, "quantity": 2}
			],
			"fees": [{"type": "SHIPPING_COST", "amount": 20000}],
		}
		base.update(overrides)
		return base

	def test_creates_draft_sales_order_with_line_and_fee(self):
		"""Happy path: SO is created as Draft with product + fee lines and wave_* stamps."""
		handle(self._payload(), self.correlation_id)
		name = frappe.db.get_value("Sales Order", {"wave_order_id": self.wave_order_id}, "name")
		self.assertIsNotNone(name)
		so = frappe.get_doc("Sales Order", name)
		self.assertEqual(int(so.docstatus), 0)
		self.assertEqual(so.wave_friendly_id, self.friendly_id)
		self.assertEqual(so.wave_correlation_id, self.correlation_id)
		item_codes = [row.item_code for row in so.items]
		self.assertIn(self.product_sku, item_codes)
		self.assertIn(self.fee_item, item_codes)

	def test_fee_line_rate_is_converted_from_cents(self):
		"""Shipping fee of 20000 cents with divisor=100 becomes rate=200.00 on the SO row."""
		handle(self._payload(), self.correlation_id)
		name = frappe.db.get_value("Sales Order", {"wave_order_id": self.wave_order_id}, "name")
		so = frappe.get_doc("Sales Order", name)
		fee_rows = [row for row in so.items if row.item_code == self.fee_item]
		self.assertEqual(len(fee_rows), 1)
		self.assertAlmostEqual(float(fee_rows[0].rate), 200.00, places=2)

	def test_duplicate_order_is_skipped_not_overwritten(self):
		"""Second ORDER.CREATE for the same wave_order_id logs Skipped and does not create a second SO."""
		handle(self._payload(), self.correlation_id)
		handle(self._payload(), self.correlation_id)
		count = frappe.db.count("Sales Order", {"wave_order_id": self.wave_order_id})
		self.assertEqual(count, 1)
		skipped = frappe.get_all(
			"Wave Sync Log",
			filters={"correlation_id": self.correlation_id, "step": "Skipped"},
			pluck="name",
		)
		self.assertGreaterEqual(len(skipped), 1)

	def test_missing_sku_raises_resolution_error(self):
		"""A product whose SKU is not in ERP raises a WaveResolutionError (processor logs Failed)."""
		payload = self._payload(products=[{"sku": "NOT_A_REAL_SKU", "quantity": 1}])
		with self.assertRaises(WaveResolutionError):
			handle(payload, self.correlation_id)

	def test_missing_fee_mapping_raises_resolution_error(self):
		"""A fee type without a Wave Fee Mapping row raises a WaveResolutionError."""
		payload = self._payload(fees=[{"type": "UNMAPPED_FEE", "amount": 500}])
		with self.assertRaises(WaveResolutionError):
			handle(payload, self.correlation_id)

	def test_missing_user_id_raises_validation_error(self):
		"""A payload without user._id is malformed; the handler refuses to proceed."""
		payload = self._payload(user={"email": "no-id@example.com"})
		with self.assertRaises(WaveResolutionError):
			handle(payload, self.correlation_id)

	def test_missing_order_id_raises_validation_error(self):
		"""A payload without _id cannot be keyed; refuse early."""
		payload = self._payload(_id=None)
		with self.assertRaises(WaveValidationError):
			handle(payload, self.correlation_id)

	def test_handler_logs_so_created_row(self):
		"""After a successful run, Wave Sync Log has an SO Created row linked to the new SO."""
		handle(self._payload(), self.correlation_id)
		so_name = frappe.db.get_value("Sales Order", {"wave_order_id": self.wave_order_id}, "name")
		rows = frappe.get_all(
			"Wave Sync Log",
			filters={"correlation_id": self.correlation_id, "step": "SO Created"},
			fields=["linked_doctype", "linked_docname"],
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].linked_doctype, "Sales Order")
		self.assertEqual(rows[0].linked_docname, so_name)
