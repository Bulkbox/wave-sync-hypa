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
		self._original_tax_rules = self._snapshot_tax_rules()
		self._clear_mappings()
		self._clear_tax_rules()
		self._ensure_defaults()
		self._add_fee_mapping("SHIPPING_COST", self.fee_item)

	def tearDown(self):
		"""Roll back any half-open transaction, then remove ERP rows this test created."""
		frappe.db.rollback()
		# Drop the SO's comments before the SO itself so no orphan references linger.
		so_names = frappe.get_all(
			"Sales Order", filters={"wave_order_id": self.wave_order_id}, pluck="name"
		)
		for so_name in so_names:
			self._safe_delete_many(
				"Comment", {"reference_doctype": "Sales Order", "reference_name": so_name}
			)
		self._safe_delete_many("Sales Order", {"wave_order_id": self.wave_order_id})
		self._safe_delete_many("Address", {"wave_address_id": self.wave_address_id})
		# A Customer's primary_contact links back to its Contact; clear it first so
		# the link check doesn't block the Contact delete.
		for cust in frappe.get_all("Customer", filters={"wave_customer_id": self.wave_user_id}, pluck="name"):
			frappe.db.set_value("Customer", cust, "customer_primary_contact", None)
		self._safe_delete_many("Contact", {"wave_contact_id": self.wave_user_id})
		self._safe_delete_many("Customer", {"wave_customer_id": self.wave_user_id})
		self._safe_delete_many("Wave Sync Log", {"correlation_id": self.correlation_id})
		self._clear_mappings()
		self._clear_tax_rules()
		self._restore_mappings(self._original_mappings)
		self._restore_tax_rules(self._original_tax_rules)

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

	def _snapshot_tax_rules(self) -> list[dict]:
		"""Capture the existing tax_rules rows so tearDown can restore them."""
		settings = frappe.get_single("Wave Settings")
		return [
			{
				"sales_taxes_and_charges_template": row.sales_taxes_and_charges_template,
				"enabled": row.enabled,
				"notes": row.notes,
			}
			for row in (settings.tax_rules or [])
		]

	def _clear_tax_rules(self) -> None:
		"""Drop every Wave Tax Rule row via direct DB delete."""
		frappe.db.delete("Wave Tax Rule", {"parent": "Wave Settings"})
		frappe.db.commit()
		frappe.clear_document_cache("Wave Settings", "Wave Settings")

	def _add_tax_rule(self, template: str, enabled: int = 1) -> None:
		"""Append a Wave Tax Rule row; bypass Link validation so tests can simulate broken references."""
		settings = frappe.get_single("Wave Settings")
		settings.append(
			"tax_rules",
			{"sales_taxes_and_charges_template": template, "enabled": enabled, "notes": "test"},
		)
		settings.flags.ignore_validate = True
		settings.flags.ignore_links = True
		settings.save(ignore_permissions=True)
		frappe.clear_document_cache("Wave Settings", "Wave Settings")

	def _restore_tax_rules(self, rows: list[dict]) -> None:
		"""Reinstate tax_rules from the snapshot."""
		settings = frappe.get_single("Wave Settings")
		settings.tax_rules = []
		for row in rows:
			settings.append("tax_rules", row)
		settings.flags.ignore_validate = True
		settings.save(ignore_permissions=True)
		frappe.clear_document_cache("Wave Settings", "Wave Settings")

	def _pick_tax_template_for_default_company(self) -> str | None:
		"""Return an enabled Sales Taxes and Charges Template matching the default company, or None."""
		company = frappe.db.get_single_value("Wave Settings", "default_company")
		if not company:
			return None
		return frappe.db.get_value(
			"Sales Taxes and Charges Template", {"company": company, "disabled": 0}, "name"
		)

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
		# Wave friendly id is stamped into Customer's Purchase Order (po_no) so it
		# shows up in the standard SO list view's PO column without operators
		# needing a custom column for wave_friendly_id.
		self.assertEqual(so.po_no, self.friendly_id)
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

	def test_missing_sku_aborts_when_no_placeholder_configured(self):
		"""All-items-unresolved with no placeholder: no SO created, abort logged."""
		payload = self._payload(products=[{"sku": "NOT_A_REAL_SKU", "quantity": 1}])
		# Ensure the placeholder setting is empty for this test.
		frappe.db.set_value(
			"Wave Settings", "Wave Settings",
			"default_unresolved_items_placeholder", None, update_modified=False,
		)
		handle(payload, self.correlation_id)
		# No Sales Order should have been created for this wave_order_id.
		self.assertIsNone(
			frappe.db.get_value("Sales Order", {"wave_order_id": self.wave_order_id}, "name")
		)
		# The abort path must write an "Aborted" Error row to Wave Sync Log.
		self.assertTrue(
			frappe.db.exists(
				"Wave Sync Log",
				{"correlation_id": self.correlation_id, "step": "Aborted", "level": "Error"},
			),
			"Expected an Aborted/Error Wave Sync Log row.",
		)

	def test_missing_fee_mapping_still_creates_draft_sales_order(self):
		"""Unmapped fee: SO drafts with products only; the unmapped fee line is absent, no raise."""
		payload = self._payload(fees=[{"type": "UNMAPPED_FEE_TYPE", "amount": 500}])
		handle(payload, self.correlation_id)
		name = frappe.db.get_value("Sales Order", {"wave_order_id": self.wave_order_id}, "name")
		self.assertIsNotNone(name, "Sales Order must still be created when a fee mapping is missing.")
		so = frappe.get_doc("Sales Order", name)
		item_codes = [row.item_code for row in so.items]
		self.assertIn(self.product_sku, item_codes, "Product line must be preserved.")
		self.assertNotIn(
			self.fee_item,
			item_codes,
			"The SHIPPING_COST fee line should be absent because the payload uses a different fee type.",
		)

	def test_missing_fee_mapping_flags_manual_review(self):
		"""An unmapped fee sets wave_manual_review_required=1 on the Sales Order for operator triage."""
		payload = self._payload(fees=[{"type": "UNMAPPED_FEE_TYPE", "amount": 500}])
		handle(payload, self.correlation_id)
		name = frappe.db.get_value("Sales Order", {"wave_order_id": self.wave_order_id}, "name")
		flag = frappe.db.get_value("Sales Order", name, "wave_manual_review_required")
		self.assertEqual(int(flag or 0), 1)

	def test_missing_fee_mapping_writes_action_required_log(self):
		"""Each skipped fee writes one Action Required / Warning row with the fee type and amount."""
		payload = self._payload(
			fees=[
				{"type": "UNMAPPED_FEE_ALPHA", "amount": 500},
				{"type": "UNMAPPED_FEE_BETA", "amount": 750},
			]
		)
		handle(payload, self.correlation_id)
		rows = frappe.get_all(
			"Wave Sync Log",
			filters={
				"correlation_id": self.correlation_id,
				"step": "Action Required",
			},
			fields=["level", "response_body", "error_message"],
		)
		self.assertEqual(len(rows), 2, "One Action Required row per skipped fee.")
		self.assertTrue(all(r.level == "Warning" for r in rows))
		combined_bodies = " ".join(r.response_body or "" for r in rows)
		self.assertIn("UNMAPPED_FEE_ALPHA", combined_bodies)
		self.assertIn("UNMAPPED_FEE_BETA", combined_bodies)

	def test_missing_fee_mapping_adds_comment_on_sales_order(self):
		"""A single descriptive Comment is attached to the SO listing every skipped fee."""
		payload = self._payload(
			fees=[
				{"type": "UNMAPPED_FEE_GAMMA", "amount": 1250},
			]
		)
		handle(payload, self.correlation_id)
		name = frappe.db.get_value("Sales Order", {"wave_order_id": self.wave_order_id}, "name")
		comments = frappe.get_all(
			"Comment",
			filters={
				"reference_doctype": "Sales Order",
				"reference_name": name,
				"comment_type": "Comment",
			},
			fields=["content"],
		)
		self.assertEqual(len(comments), 1)
		body = comments[0].content
		self.assertIn("UNMAPPED_FEE_GAMMA", body)
		self.assertIn("Fee Mappings", body)
		self.assertIn(self.wave_order_id, body)

	def test_resolvable_fees_mixed_with_unmapped_still_add_the_resolvable_lines(self):
		"""A mix of mapped and unmapped fees: the mapped fee lands as a line; the unmapped one is skipped + logged."""
		payload = self._payload(
			fees=[
				{"type": "SHIPPING_COST", "amount": 20000},
				{"type": "UNMAPPED_FEE_DELTA", "amount": 500},
			]
		)
		handle(payload, self.correlation_id)
		name = frappe.db.get_value("Sales Order", {"wave_order_id": self.wave_order_id}, "name")
		so = frappe.get_doc("Sales Order", name)
		fee_rows = [row for row in so.items if row.item_code == self.fee_item]
		self.assertEqual(len(fee_rows), 1, "Mapped SHIPPING_COST fee must still be added.")
		action_logs = frappe.get_all(
			"Wave Sync Log",
			filters={"correlation_id": self.correlation_id, "step": "Action Required"},
		)
		self.assertEqual(len(action_logs), 1, "One Action Required row for the unmapped fee.")

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

	def test_tax_rule_enabled_stamps_template_and_populates_taxes(self):
		"""An enabled Wave Tax Rule puts taxes_and_charges on the SO and ERPNext copies the template's rows."""
		template = self._pick_tax_template_for_default_company()
		if not template:
			self.skipTest("No Sales Taxes and Charges Template available for the default company.")
		self._add_tax_rule(template)
		handle(self._payload(), self.correlation_id)
		name = frappe.db.get_value("Sales Order", {"wave_order_id": self.wave_order_id}, "name")
		so = frappe.get_doc("Sales Order", name)
		self.assertEqual(so.taxes_and_charges, template)
		self.assertGreaterEqual(
			len(so.taxes),
			1,
			"ERPNext should have auto-populated the SO's taxes table from the template.",
		)

	def test_no_tax_rule_results_in_so_without_template(self):
		"""With no tax rules configured the handler does not set taxes_and_charges.

		ERPNext (via kenya_compliance_via_slade or a default tax mechanism) may still
		auto-populate the SO's taxes table from other sources; we only assert on the
		field this handler controls.
		"""
		handle(self._payload(), self.correlation_id)
		name = frappe.db.get_value("Sales Order", {"wave_order_id": self.wave_order_id}, "name")
		so = frappe.get_doc("Sales Order", name)
		self.assertFalse(so.taxes_and_charges)

	def test_disabled_tax_rule_is_ignored(self):
		"""A disabled Wave Tax Rule is as good as absent; no template is applied."""
		template = self._pick_tax_template_for_default_company()
		if not template:
			self.skipTest("No Sales Taxes and Charges Template available for the default company.")
		self._add_tax_rule(template, enabled=0)
		handle(self._payload(), self.correlation_id)
		name = frappe.db.get_value("Sales Order", {"wave_order_id": self.wave_order_id}, "name")
		so = frappe.get_doc("Sales Order", name)
		self.assertFalse(so.taxes_and_charges)

	def test_missing_template_soft_fails_with_log_flag_and_comment(self):
		"""A rule referencing a non-existent template drafts the SO without taxes, flags review, logs, comments."""
		self._add_tax_rule("WAVE_SYNC_NONEXISTENT_TEMPLATE")
		handle(self._payload(), self.correlation_id)
		name = frappe.db.get_value("Sales Order", {"wave_order_id": self.wave_order_id}, "name")
		self.assertIsNotNone(name, "SO must still be created when the tax template is missing.")
		so = frappe.get_doc("Sales Order", name)
		self.assertFalse(so.taxes_and_charges)
		self.assertEqual(int(so.wave_manual_review_required or 0), 1)
		logs = frappe.get_all(
			"Wave Sync Log",
			filters={"correlation_id": self.correlation_id, "step": "Action Required"},
			fields=["response_body", "error_message"],
		)
		self.assertEqual(len(logs), 1)
		self.assertIn("template_missing", logs[0].response_body or "")
		comments = frappe.get_all(
			"Comment",
			filters={
				"reference_doctype": "Sales Order",
				"reference_name": name,
				"comment_type": "Comment",
			},
			fields=["content"],
		)
		self.assertEqual(len(comments), 1)
		self.assertIn("Tax Rules", comments[0].content)
		self.assertIn("WAVE_SYNC_NONEXISTENT_TEMPLATE", comments[0].content)
