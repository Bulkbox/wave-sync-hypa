"""HTTP-shape tests for wave_client.get_admin_product_by_id + create_admin_order.

Both endpoints are needed by the ERP -> Wave order push: GET the admin
catalog data per SKU to backfill OrderProductV3 fields, then POST the
assembled order. Tests patch `requests.get` / `requests.post` at the
module boundary so no real HTTP fires.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import requests
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.services import wave_client
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveOutboundError

BASE_URL = "https://wave.example.com"
API_KEY = "test-api-key-456"
APP_ID = "test-app-id"
PRODUCT_ID = "69e0d857fe91acfd81c57396"


class TestGetAdminProductById(FrappeTestCase):
	"""Wrapper for GET /api/v3/admin/products/{id}."""

	def test_200_returns_parsed_dict(self):
		fake_body = {"_id": PRODUCT_ID, "sku": "JTD011", "name": [{"language": "en", "text": "Test"}]}
		fake_response = MagicMock(status_code=200, content=b'{"_id":"x"}')
		fake_response.json.return_value = fake_body
		with patch.object(requests, "get", return_value=fake_response) as mock_get:
			result = wave_client.get_admin_product_by_id(
				base_url=BASE_URL, api_key=API_KEY, app_id=APP_ID, product_id=PRODUCT_ID,
			)
		self.assertEqual(result, fake_body)
		args, kwargs = mock_get.call_args
		self.assertEqual(args[0], f"{BASE_URL}/api/v3/admin/products/{PRODUCT_ID}")
		self.assertEqual(kwargs["headers"]["X-API-Key"], API_KEY)
		self.assertEqual(kwargs["headers"]["appId"], APP_ID)

	def test_404_returns_none_not_exception(self):
		"""Wave deleted the product since we cached it -> caller distinguishes from HTTP errors."""
		fake_response = MagicMock(status_code=404, text="not found", content=b"not found")
		fake_response.json.side_effect = ValueError("not json")
		with patch.object(requests, "get", return_value=fake_response):
			result = wave_client.get_admin_product_by_id(
				base_url=BASE_URL, api_key=API_KEY, app_id=APP_ID, product_id=PRODUCT_ID,
			)
		self.assertIsNone(result)

	def test_5xx_raises_outbound_error(self):
		fake_response = MagicMock(status_code=503, text="upstream broken", content=b"upstream broken")
		fake_response.json.side_effect = ValueError("not json")
		with patch.object(requests, "get", return_value=fake_response):
			with self.assertRaises(WaveOutboundError) as ctx:
				wave_client.get_admin_product_by_id(
					base_url=BASE_URL, api_key=API_KEY, app_id=APP_ID, product_id=PRODUCT_ID,
				)
		self.assertEqual(ctx.exception.http_status, 503)

	def test_network_error_wrapped(self):
		with patch.object(requests, "get", side_effect=requests.ConnectionError("dns fail")):
			with self.assertRaises(WaveOutboundError) as ctx:
				wave_client.get_admin_product_by_id(
					base_url=BASE_URL, api_key=API_KEY, app_id=APP_ID, product_id=PRODUCT_ID,
				)
		self.assertIn("network error", str(ctx.exception))

	def test_empty_product_id_rejected_without_http(self):
		with patch.object(requests, "get") as mock_get:
			with self.assertRaises(WaveOutboundError):
				wave_client.get_admin_product_by_id(
					base_url=BASE_URL, api_key=API_KEY, app_id=APP_ID, product_id="",
				)
		mock_get.assert_not_called()


class TestCreateAdminOrder(FrappeTestCase):
	"""Wrapper for POST /api/v3/admin/orders?skipWebhookNotification=true."""

	def _body(self) -> dict:
		"""Minimum body that Wave's validator accepts (confirmed via probe)."""
		return {
			"integratorId": "SAL-ORD-001",
			"userId": "wave-cust-1",
			"shopId": "wave-shop-1",
			"products": [{"productId": "wave-prod-1", "quantity": 1, "beginPrice": 1000, "finalPrice": 1000, "sku": "X"}],
			"paymentType": "cash",
			"paymentStatus": "PENDING",
			"status": "PENDING",
			"orderType": "ORDER",
			"totalPrice": 1000,
			"orderItemsPrice": 1000,
			"paymentManagedByIntegrator": True,
			"deliveryService": "standard",
		}

	def test_201_returns_parsed_response_with_id(self):
		"""Happy path: POST succeeds; client gets back the new order with _id + friendlyId."""
		response_body = {"_id": "wave-order-aaa", "friendlyId": "10000099", "status": "PENDING"}
		fake_response = MagicMock(status_code=201, content=b'{"_id":"x"}')
		fake_response.json.return_value = response_body
		with patch.object(requests, "post", return_value=fake_response) as mock_post:
			result = wave_client.create_admin_order(
				base_url=BASE_URL, api_key=API_KEY, app_id=APP_ID, body=self._body(),
			)
		self.assertEqual(result, response_body)
		args, kwargs = mock_post.call_args
		self.assertEqual(args[0], f"{BASE_URL}/api/v3/admin/orders")
		# Default skip_webhook_notification=True is reflected in query params.
		self.assertEqual(kwargs["params"]["skipWebhookNotification"], "true")

	def test_skip_webhook_notification_false_passes_through(self):
		fake_response = MagicMock(status_code=201, content=b'{"_id":"x"}')
		fake_response.json.return_value = {"_id": "x"}
		with patch.object(requests, "post", return_value=fake_response) as mock_post:
			wave_client.create_admin_order(
				base_url=BASE_URL, api_key=API_KEY, app_id=APP_ID, body=self._body(),
				skip_webhook_notification=False,
			)
		self.assertEqual(mock_post.call_args.kwargs["params"]["skipWebhookNotification"], "false")

	def test_422_raises_with_wave_code(self):
		"""Wave validation error like ORDER0009 surfaces as WaveOutboundError with wave_code."""
		envelope = {"code": "ORDER0009", "userMessage": "Delivery service not found"}
		fake_response = MagicMock(status_code=422)
		fake_response.text = '{"code":"ORDER0009"}'
		fake_response.content = b'{"code":"ORDER0009"}'
		fake_response.json.return_value = envelope
		with patch.object(requests, "post", return_value=fake_response):
			with self.assertRaises(WaveOutboundError) as ctx:
				wave_client.create_admin_order(
					base_url=BASE_URL, api_key=API_KEY, app_id=APP_ID, body=self._body(),
				)
		self.assertEqual(ctx.exception.http_status, 422)
		self.assertEqual(ctx.exception.wave_code, "ORDER0009")

	def test_network_error_wrapped(self):
		with patch.object(requests, "post", side_effect=requests.ConnectionError("dns fail")):
			with self.assertRaises(WaveOutboundError) as ctx:
				wave_client.create_admin_order(
					base_url=BASE_URL, api_key=API_KEY, app_id=APP_ID, body=self._body(),
				)
		self.assertIn("network error", str(ctx.exception))

	def test_empty_body_rejected_without_http(self):
		with patch.object(requests, "post") as mock_post:
			with self.assertRaises(WaveOutboundError):
				wave_client.create_admin_order(
					base_url=BASE_URL, api_key=API_KEY, app_id=APP_ID, body={},
				)
		mock_post.assert_not_called()


class TestPatchOrderTopLevel(FrappeTestCase):
	"""Wrapper for PATCH /api/v3/admin/orders/{id} (order-level scalars).

	Sibling of patch_order_products — that one mutates the line items
	array; this one mutates order-level fields like pickerStatus + picking.
	"""

	ORDER_ID = "wave-order-zzz"

	def test_200_returns_parsed_dict_and_sends_dict_body(self):
		body = {"pickerStatus": None, "picking": None}
		response_body = {"_id": self.ORDER_ID, "pickerStatus": None, "status": "ACCEPTED"}
		fake_response = MagicMock(status_code=200, content=b'{"_id":"x"}')
		fake_response.json.return_value = response_body
		with patch.object(requests, "patch", return_value=fake_response) as mock_patch:
			result = wave_client.patch_order_top_level(
				base_url=BASE_URL, api_key=API_KEY, app_id=APP_ID,
				order_id=self.ORDER_ID, body=body,
			)
		self.assertEqual(result, response_body)
		args, kwargs = mock_patch.call_args
		self.assertEqual(args[0], f"{BASE_URL}/api/v3/admin/orders/{self.ORDER_ID}")
		self.assertEqual(kwargs["json"], body)
		self.assertEqual(kwargs["headers"]["X-API-Key"], API_KEY)
		self.assertEqual(kwargs["headers"]["appId"], APP_ID)

	def test_422_raises_with_wave_code(self):
		envelope = {"code": "ORDER0099", "userMessage": "field not writable"}
		fake_response = MagicMock(status_code=422)
		fake_response.text = '{"code":"ORDER0099"}'
		fake_response.content = b'{"code":"ORDER0099"}'
		fake_response.json.return_value = envelope
		with patch.object(requests, "patch", return_value=fake_response):
			with self.assertRaises(WaveOutboundError) as ctx:
				wave_client.patch_order_top_level(
					base_url=BASE_URL, api_key=API_KEY, app_id=APP_ID,
					order_id=self.ORDER_ID, body={"pickerStatus": None},
				)
		self.assertEqual(ctx.exception.http_status, 422)
		self.assertEqual(ctx.exception.wave_code, "ORDER0099")

	def test_network_error_wrapped(self):
		with patch.object(requests, "patch", side_effect=requests.ConnectionError("dns fail")):
			with self.assertRaises(WaveOutboundError) as ctx:
				wave_client.patch_order_top_level(
					base_url=BASE_URL, api_key=API_KEY, app_id=APP_ID,
					order_id=self.ORDER_ID, body={"pickerStatus": None},
				)
		self.assertIn("network error", str(ctx.exception))

	def test_empty_body_rejected_without_http(self):
		with patch.object(requests, "patch") as mock_patch:
			with self.assertRaises(WaveOutboundError):
				wave_client.patch_order_top_level(
					base_url=BASE_URL, api_key=API_KEY, app_id=APP_ID,
					order_id=self.ORDER_ID, body={},
				)
		mock_patch.assert_not_called()

	def test_empty_order_id_rejected_without_http(self):
		with patch.object(requests, "patch") as mock_patch:
			with self.assertRaises(WaveOutboundError):
				wave_client.patch_order_top_level(
					base_url=BASE_URL, api_key=API_KEY, app_id=APP_ID,
					order_id="", body={"pickerStatus": None},
				)
		mock_patch.assert_not_called()
