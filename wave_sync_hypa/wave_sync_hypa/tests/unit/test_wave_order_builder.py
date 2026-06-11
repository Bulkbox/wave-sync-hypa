"""Unit tests for services.wave_order_builder.build_order_payload.

Mocks the three external surfaces — frappe.db for the Item.wave_product_id
cache, product_resolver for by-sku fallbacks, and wave_client for the admin
product GET — so the builder's branching is exercised in pure form.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.services import wave_order_builder
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveOutboundError, WaveResolutionError

CONFIG = {"base_url": "https://wave.example.com", "api_key": "key", "app_id": "app"}


def _settings(
	divisor: int = 100,
	common_offline_id: str = "wave-cust-1",
	shop_id: str = "wave-shop-1",
	fee_mappings: list | None = None,
) -> MagicMock:
	settings = MagicMock(name="WaveSettings")
	settings.get.side_effect = lambda key, default=None: {
		"price_scale_divisor": divisor,
		"wave_common_offline_customer_id": common_offline_id,
		"wave_shop_id": shop_id,
		"wave_default_offline_payment_type": "cash",
		"fee_mappings": fee_mappings or [],
	}.get(key, default)
	return settings


def _so(items: list[dict], name: str = "SAL-ORD-001", wave_comments: str = "") -> SimpleNamespace:
	values = {"items": items, "name": name, "wave_comments": wave_comments}
	doc = SimpleNamespace(name=name)
	doc.get = lambda key, default=None: values.get(key, default)
	return doc


def _catalog(name_text: str = "JTD011 — Test Item", vat: int = 16) -> dict:
	"""Minimal Wave admin product catalog response stub."""
	return {
		"_id": "wave-prod-1",
		"sku": "JTD011",
		"name": [{"language": "en", "text": name_text}],
		"categories": ["cat-1"],
		"uom": [{"language": "en", "text": "PCS"}],
		"unitOfMeasurement": [{"language": "en", "text": ""}],
		"unitOfMeasurementBaseCoefficient": 1000,
		"vat": vat,
		"isWeighed": False,
		"stepToUom": 1,
	}


class TestBuildOrderPayloadHappyPath(FrappeTestCase):
	"""Single line, cached wave_product_id, catalog GET succeeds → well-formed body."""

	def test_single_line_produces_correct_top_level_body(self):
		items = [{"item_code": "JTD011", "qty": 2, "rate": 100.0, "amount": 200.0}]
		so = _so(items)
		with (
			patch.object(frappe.db, "get_value", return_value="wave-prod-1"),
			patch.object(wave_order_builder.wave_client, "get_admin_product_by_id", return_value=_catalog()) as mock_get,
		):
			body = wave_order_builder.build_order_payload(so, "wave-cust-1", _settings(), "corr-1", CONFIG)

		mock_get.assert_called_once_with(
			base_url=CONFIG["base_url"], api_key=CONFIG["api_key"], app_id=CONFIG["app_id"],
			product_id="wave-prod-1",
		)
		# Top-level shape mirrors the locked spec from the probe.
		self.assertEqual(body["integratorId"], "SAL-ORD-001")
		self.assertEqual(body["userId"], "wave-cust-1")
		self.assertEqual(body["shopId"], "wave-shop-1")
		self.assertEqual(body["paymentType"], "cash")
		self.assertEqual(body["paymentStatus"], "PENDING")
		self.assertEqual(body["status"], "PENDING")
		self.assertEqual(body["orderType"], "ORDER")
		self.assertEqual(body["paymentManagedByIntegrator"], True)
		self.assertEqual(body["deliveryService"], "standard")
		# Prices: rate=100 major × divisor=100 → 10000 cents per unit; amount=200 × 100 = 20000.
		self.assertEqual(body["totalPrice"], 20000)
		self.assertEqual(body["orderItemsPrice"], 20000)

	def test_product_line_merges_erp_and_wave_catalog(self):
		items = [{"item_code": "JTD011", "qty": 5, "rate": 100.0, "amount": 500.0}]
		so = _so(items)
		with (
			patch.object(frappe.db, "get_value", return_value="wave-prod-1"),
			patch.object(wave_order_builder.wave_client, "get_admin_product_by_id", return_value=_catalog()),
		):
			body = wave_order_builder.build_order_payload(so, "wave-cust-1", _settings(), "corr-1", CONFIG)
		line = body["products"][0]
		# ERP-supplied fields:
		self.assertEqual(line["productId"], "wave-prod-1")
		self.assertEqual(line["sku"], "JTD011")
		self.assertEqual(line["quantity"], 5)
		self.assertEqual(line["beginPrice"], 10000)  # 100 × 100
		self.assertEqual(line["finalPrice"], 10000)
		# Wave-supplied catalog fields:
		self.assertEqual(line["name"], [{"language": "en", "text": "JTD011 — Test Item"}])
		self.assertEqual(line["categories"], ["cat-1"])
		self.assertEqual(line["uom"], [{"language": "en", "text": "PCS"}])
		self.assertEqual(line["unitOfMeasurementBaseCoefficient"], 1000)
		self.assertEqual(line["vat"], 16)
		self.assertEqual(line["isWeighed"], False)
		self.assertEqual(line["stepToUom"], 1)

	def test_multiple_distinct_skus_each_get_one_catalog_call(self):
		"""Two SO lines with different SKUs → two GETs, not one or three."""
		items = [
			{"item_code": "JTD011", "qty": 2, "rate": 100.0, "amount": 200.0},
			{"item_code": "BFK162", "qty": 1, "rate": 50.0, "amount": 50.0},
		]

		def _resolver(*args, **kwargs):
			return {"JTD011": "wave-prod-jtd", "BFK162": "wave-prod-bfk"}[args[1]]

		def _catalog_fn(**kwargs):
			pid = kwargs["product_id"]
			cat = _catalog()
			cat["_id"] = pid
			cat["sku"] = "JTD011" if pid == "wave-prod-jtd" else "BFK162"
			return cat

		with (
			patch.object(frappe.db, "get_value", side_effect=_resolver),
			patch.object(wave_order_builder.wave_client, "get_admin_product_by_id", side_effect=_catalog_fn) as mock_get,
		):
			body = wave_order_builder.build_order_payload(_so(items), "wave-cust-1", _settings(), "corr-1", CONFIG)
		self.assertEqual(mock_get.call_count, 2)
		self.assertEqual([p["sku"] for p in body["products"]], ["JTD011", "BFK162"])
		# orderItemsPrice = 20000 + 5000 = 25000.
		self.assertEqual(body["totalPrice"], 25000)

	def test_duplicate_skus_dedup_to_one_catalog_call(self):
		"""Same SKU twice on the SO → one GET; both lines populated from same catalog."""
		items = [
			{"item_code": "JTD011", "qty": 1, "rate": 100.0, "amount": 100.0},
			{"item_code": "JTD011", "qty": 2, "rate": 100.0, "amount": 200.0},
		]
		with (
			patch.object(frappe.db, "get_value", return_value="wave-prod-1"),
			patch.object(wave_order_builder.wave_client, "get_admin_product_by_id", return_value=_catalog()) as mock_get,
		):
			body = wave_order_builder.build_order_payload(_so(items), "wave-cust-1", _settings(), "corr-1", CONFIG)
		self.assertEqual(mock_get.call_count, 1)
		self.assertEqual(len(body["products"]), 2)


class TestBuildOrderPayloadFailures(FrappeTestCase):
	"""Pre-flight surfaces all unresolvable SKUs; catalog 404 -> WaveOutboundError."""

	def test_unresolvable_skus_listed_in_one_error(self):
		items = [
			{"item_code": "JTD011", "qty": 1, "rate": 100.0, "amount": 100.0},
			{"item_code": "MISSING-1", "qty": 1, "rate": 50.0, "amount": 50.0},
			{"item_code": "MISSING-2", "qty": 1, "rate": 50.0, "amount": 50.0},
		]

		def _cache_lookup(*args, **kwargs):
			return {"JTD011": "wave-prod-1", "MISSING-1": None, "MISSING-2": None}.get(args[1])

		with (
			patch.object(frappe.db, "get_value", side_effect=_cache_lookup),
			patch.object(wave_order_builder.product_resolver, "resolve_wave_product_id", return_value=None),
			patch.object(wave_order_builder.wave_client, "get_admin_product_by_id") as mock_get,
		):
			with self.assertRaises(WaveResolutionError) as ctx:
				wave_order_builder.build_order_payload(_so(items), "wave-cust-1", _settings(), "corr-1", CONFIG)
		# Both missing SKUs listed; no catalog GET fired.
		msg = str(ctx.exception)
		self.assertIn("MISSING-1", msg)
		self.assertIn("MISSING-2", msg)
		mock_get.assert_not_called()

	def test_catalog_404_raises_wave_outbound_error_with_code(self):
		items = [{"item_code": "JTD011", "qty": 1, "rate": 100.0, "amount": 100.0}]
		with (
			patch.object(frappe.db, "get_value", return_value="wave-prod-stale"),
			patch.object(wave_order_builder.wave_client, "get_admin_product_by_id", return_value=None),
		):
			with self.assertRaises(WaveOutboundError) as ctx:
				wave_order_builder.build_order_payload(_so(items), "wave-cust-1", _settings(), "corr-1", CONFIG)
		self.assertEqual(ctx.exception.http_status, 404)
		self.assertEqual(ctx.exception.wave_code, "PRODUCT_NOT_FOUND")
		self.assertIn("JTD011", str(ctx.exception))


class TestBuildOrderPayloadComments(FrappeTestCase):
	"""wave_comments wins over the default; blank comments use the SO-name template."""

	def test_existing_wave_comments_passes_through(self):
		items = [{"item_code": "JTD011", "qty": 1, "rate": 100.0, "amount": 100.0}]
		so = _so(items, wave_comments="Deliver to side gate")
		with (
			patch.object(frappe.db, "get_value", return_value="wave-prod-1"),
			patch.object(wave_order_builder.wave_client, "get_admin_product_by_id", return_value=_catalog()),
		):
			body = wave_order_builder.build_order_payload(so, "wave-cust-1", _settings(), "corr-1", CONFIG)
		self.assertEqual(body["comments"], "Deliver to side gate")

	def test_blank_wave_comments_falls_back_to_default(self):
		items = [{"item_code": "JTD011", "qty": 1, "rate": 100.0, "amount": 100.0}]
		with (
			patch.object(frappe.db, "get_value", return_value="wave-prod-1"),
			patch.object(wave_order_builder.wave_client, "get_admin_product_by_id", return_value=_catalog()),
		):
			body = wave_order_builder.build_order_payload(_so(items), "wave-cust-1", _settings(), "corr-1", CONFIG)
		self.assertIn("SAL-ORD-001", body["comments"])
		self.assertIn("ERP-pushed", body["comments"])


class TestFeeItemsExcludedFromPush(FrappeTestCase):
	"""Fee/shipping lines (Wave Settings.fee_mappings) are never pushed as Wave products."""

	def test_fee_line_excluded_from_products_and_totals(self):
		items = [
			{"item_code": "JTD011", "qty": 2, "rate": 100.0, "amount": 200.0},
			{"item_code": "Shipping Cost", "qty": 1, "rate": 50.0, "amount": 50.0},
		]
		so = _so(items)
		settings = _settings(fee_mappings=[{"erp_item_code": "Shipping Cost"}])
		with (
			patch.object(frappe.db, "get_value", return_value="wave-prod-1"),
			patch.object(
				wave_order_builder.wave_client, "get_admin_product_by_id", return_value=_catalog()
			) as mock_get,
		):
			body = wave_order_builder.build_order_payload(so, "wave-cust-1", settings, "corr-fee", CONFIG)
		# Only the real product is pushed; the shipping line is not sent to Wave.
		self.assertEqual([p["sku"] for p in body["products"]], ["JTD011"])
		# Catalog GET fired only for the product, never for the fee item.
		mock_get.assert_called_once()
		# Totals reflect products only (fee amount excluded): 200 × 100.
		self.assertEqual(body["orderItemsPrice"], 20000)
		self.assertEqual(body["totalPrice"], 20000)
