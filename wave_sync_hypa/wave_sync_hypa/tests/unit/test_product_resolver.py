"""Unit tests for services/product_resolver.

The resolver is the only place that translates ERP `Item.item_code` into
Wave's internal product `_id`. It owns three behaviours that the stock
pusher relies on:

  1. On a 200 response carrying _id -> persist the id on Item, return it.
  2. On Wave's "not found" convention (200 + empty body) -> return None
     and emit an operator-actionable Error Log + Wave Sync Log row.
  3. On any other Wave error -> return None (never raise) and log it.

These tests pin those behaviours with the wave_client.get_product_by_sku
function patched at the module boundary, so they don't touch real HTTP.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.services import product_resolver
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveOutboundError

DUMMY_BASE_URL = "https://wave.example.com"
DUMMY_API_KEY = "k" * 32
DUMMY_APP_ID = "test-app-id"
DUMMY_ITEM = "JTD011"
DUMMY_WAVE_ID = "69e0d857fe91acfd81c57396"


def _stub_settings(*, base_url=DUMMY_BASE_URL, app_id=DUMMY_APP_ID, api_key=DUMMY_API_KEY) -> MagicMock:
	"""Mimic just enough of Wave Settings for the resolver's config check."""
	settings = MagicMock(name="WaveSettings")
	values = {"wave_api_base_url": base_url, "wave_app_id": app_id}
	settings.get.side_effect = lambda key, default=None: values.get(key, default)
	settings.get_password.return_value = api_key
	return settings


class TestResolveWaveProductId(FrappeTestCase):
	"""resolve_wave_product_id() — the core resolver entrypoint."""

	def test_resolves_and_persists_id_on_200(self):
		"""200 with _id -> resolver saves it via frappe.db.set_value and returns the id."""
		body = {"_id": DUMMY_WAVE_ID, "sku": DUMMY_ITEM, "name": "Test Product"}
		with (
			patch.object(
				product_resolver.wave_client,
				"get_product_by_sku",
				return_value=body,
			) as mock_get,
			patch.object(frappe.db, "set_value") as mock_set,
			patch.object(product_resolver, "log_step") as mock_log,
		):
			result = product_resolver.resolve_wave_product_id(
				DUMMY_ITEM, _stub_settings(), "corr-1"
			)

		self.assertEqual(result, DUMMY_WAVE_ID)
		mock_get.assert_called_once_with(
			base_url=DUMMY_BASE_URL,
			api_key=DUMMY_API_KEY,
			app_id=DUMMY_APP_ID,
			sku=DUMMY_ITEM,
		)
		mock_set.assert_called_once_with(
			"Item", DUMMY_ITEM, "wave_product_id", DUMMY_WAVE_ID, update_modified=False
		)
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(product_resolver.STEP_RESOLVE_ATTEMPT, steps)
		self.assertIn(product_resolver.STEP_RESOLVE_SUCCESS, steps)

	def test_does_not_persist_when_persist_false(self):
		"""persist=False (admin preview) returns the id without writing it back."""
		body = {"_id": DUMMY_WAVE_ID, "sku": DUMMY_ITEM}
		with (
			patch.object(product_resolver.wave_client, "get_product_by_sku", return_value=body),
			patch.object(frappe.db, "set_value") as mock_set,
			patch.object(product_resolver, "log_step"),
		):
			result = product_resolver.resolve_wave_product_id(
				DUMMY_ITEM, _stub_settings(), "corr-preview", persist=False
			)

		self.assertEqual(result, DUMMY_WAVE_ID)
		mock_set.assert_not_called()

	def test_returns_none_and_alerts_when_wave_has_no_match(self):
		"""Empty Wave response -> None, log_step at Error level + frappe.log_error fire."""
		with (
			patch.object(product_resolver.wave_client, "get_product_by_sku", return_value=None),
			patch.object(frappe.db, "set_value") as mock_set,
			patch.object(product_resolver, "log_step") as mock_log,
			patch.object(frappe, "log_error") as mock_error_log,
		):
			result = product_resolver.resolve_wave_product_id(
				"NONEXISTENT", _stub_settings(), "corr-missing"
			)

		self.assertIsNone(result)
		mock_set.assert_not_called()
		not_found_calls = [
			c for c in mock_log.call_args_list
			if c.kwargs.get("step") == product_resolver.STEP_RESOLVE_NOT_FOUND
		]
		self.assertEqual(len(not_found_calls), 1)
		self.assertEqual(not_found_calls[0].kwargs.get("level"), "Error")
		mock_error_log.assert_called_once()

	def test_returns_none_on_outbound_error(self):
		"""WaveOutboundError from the client -> None and an Error log row, no raise."""
		with (
			patch.object(
				product_resolver.wave_client,
				"get_product_by_sku",
				side_effect=WaveOutboundError("HTTP 503: upstream", http_status=503),
			),
			patch.object(frappe.db, "set_value") as mock_set,
			patch.object(product_resolver, "log_step") as mock_log,
		):
			# Must not raise.
			result = product_resolver.resolve_wave_product_id(
				DUMMY_ITEM, _stub_settings(), "corr-503"
			)

		self.assertIsNone(result)
		mock_set.assert_not_called()
		failure_calls = [
			c for c in mock_log.call_args_list
			if c.kwargs.get("step") == product_resolver.STEP_RESOLVE_SEARCH_FAILED
		]
		self.assertEqual(len(failure_calls), 1)
		self.assertEqual(failure_calls[0].kwargs.get("level"), "Error")

	def test_aborts_on_incomplete_outbound_config(self):
		"""Empty wave_app_id -> resolver returns None without ever hitting the network."""
		settings = _stub_settings(app_id="")
		with (
			patch.object(product_resolver.wave_client, "get_product_by_sku") as mock_get,
			patch.object(product_resolver, "log_step") as mock_log,
		):
			result = product_resolver.resolve_wave_product_id(
				DUMMY_ITEM, settings, "corr-noconfig"
			)

		self.assertIsNone(result)
		mock_get.assert_not_called()
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(product_resolver.STEP_RESOLVE_SEARCH_FAILED, steps)
