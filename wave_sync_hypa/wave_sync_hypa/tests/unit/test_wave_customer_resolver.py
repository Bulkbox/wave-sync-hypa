"""Unit tests for services.wave_customer_resolver.resolve_wave_customer_for_so.

Two-branch resolver, no HTTP. Tests pin each branch + the error case.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.services import wave_customer_resolver
from wave_sync_hypa.wave_sync_hypa.utils.errors import WaveResolutionError

CACHED_WAVE_ID = "wave-customer-cached-1234"
DEFAULT_WAVE_ID = "wave-customer-default-9999"


def _settings(common_offline_id: str = "") -> MagicMock:
	settings = MagicMock(name="WaveSettings")
	settings.get.side_effect = lambda key, default=None: {
		"wave_common_offline_customer_id": common_offline_id,
	}.get(key, default)
	return settings


def _so(customer: str = "CUST-001", name: str = "SAL-ORD-001") -> SimpleNamespace:
	doc = SimpleNamespace(customer=customer, name=name)
	doc.get = lambda key, default=None: getattr(doc, key, default)
	return doc


class TestResolveWaveCustomerForSo(FrappeTestCase):
	"""Three branches: cached id wins, fallback default, hard error."""

	def test_cached_wave_customer_id_is_returned_first(self):
		with patch.object(frappe.db, "get_value", return_value=CACHED_WAVE_ID) as mock_get:
			result = wave_customer_resolver.resolve_wave_customer_for_so(
				_so(), _settings(common_offline_id=DEFAULT_WAVE_ID),
			)
		self.assertEqual(result, CACHED_WAVE_ID)
		mock_get.assert_called_once_with("Customer", "CUST-001", "wave_customer_id")

	def test_blank_cache_falls_back_to_configured_default(self):
		with patch.object(frappe.db, "get_value", return_value=""):
			result = wave_customer_resolver.resolve_wave_customer_for_so(
				_so(), _settings(common_offline_id=DEFAULT_WAVE_ID),
			)
		self.assertEqual(result, DEFAULT_WAVE_ID)

	def test_no_cache_and_no_default_raises_with_actionable_message(self):
		with patch.object(frappe.db, "get_value", return_value=""):
			with self.assertRaises(WaveResolutionError) as ctx:
				wave_customer_resolver.resolve_wave_customer_for_so(_so(), _settings(common_offline_id=""))
		msg = str(ctx.exception)
		# Message names the SO + the Customer + tells the operator both fixes.
		self.assertIn("SAL-ORD-001", msg)
		self.assertIn("CUST-001", msg)
		self.assertIn("wave_customer_id", msg)
		self.assertIn("Common Offline Customer", msg)

	def test_so_with_no_customer_skips_cache_check_uses_default(self):
		"""Edge: blank SO.customer goes straight to the default fallback."""
		with patch.object(frappe.db, "get_value") as mock_get:
			result = wave_customer_resolver.resolve_wave_customer_for_so(
				_so(customer=""), _settings(common_offline_id=DEFAULT_WAVE_ID),
			)
		self.assertEqual(result, DEFAULT_WAVE_ID)
		# Cache lookup skipped when there's no Customer to look up against.
		mock_get.assert_not_called()
