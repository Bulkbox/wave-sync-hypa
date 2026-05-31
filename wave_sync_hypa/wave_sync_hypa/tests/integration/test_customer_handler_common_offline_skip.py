"""Pure unit test for the CUSTOMER.UPDATE short-circuit on the common offline customer.

The handler must never mirror CUSTOMER.UPDATE webhooks for the Wave-side
placeholder customer used as the fallback for ERP-pushed orders. This file
patches frappe at the module boundary and asserts that:

  1. When wave_common_offline_customer_id is configured AND the payload's _id
     matches it, the handler logs Info and returns BEFORE calling any
     resolver (find_or_create_customer / apply_customer_updates / etc).

  2. When the field is blank, the short-circuit never fires.

  3. When the payload _id differs from the configured value, normal flow runs.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers import customer as customer_handler

COMMON_OFFLINE_ID = "wave-common-offline-customer-id"
OTHER_CUSTOMER_ID = "wave-real-customer-id"


def _settings(common_offline_id: str = COMMON_OFFLINE_ID) -> MagicMock:
	settings = MagicMock(name="WaveSettings")
	settings.get.side_effect = lambda key, default=None: {
		"wave_common_offline_customer_id": common_offline_id,
	}.get(key, default)
	return settings


class TestCommonOfflineCustomerShortCircuit(FrappeTestCase):
	"""Guard clause at the top of handle() prevents ERP-side mirroring of the placeholder."""

	def test_matching_id_short_circuits_before_resolver(self):
		"""Payload _id == configured common offline id -> Info log + return; no resolver runs."""
		payload = {"_id": COMMON_OFFLINE_ID, "updatedAt": "2026-05-20T00:00:00Z"}
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(customer_handler, "find_or_create_customer") as mock_resolver,
			patch.object(customer_handler, "log_step") as mock_log,
		):
			customer_handler.handle(payload, "corr-skip")
		mock_resolver.assert_not_called()
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertEqual(steps, ["Skipped"])
		# The skip row carries the reason so operators can audit it.
		response_body = mock_log.call_args_list[0].kwargs.get("response_body") or {}
		self.assertEqual(response_body.get("reason"), "common_offline_customer_no_op")

	def test_blank_setting_never_short_circuits(self):
		"""Setting blank -> handler runs the full resolver path; no skip log."""
		payload = {"_id": OTHER_CUSTOMER_ID, "updatedAt": "2026-05-20T00:00:00Z"}
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(common_offline_id="")),
			patch.object(
				customer_handler,
				"find_or_create_customer",
				return_value=("CUST-X", False, "wave_id"),
			) as mock_resolver,
			patch.object(customer_handler, "apply_customer_updates"),
			patch.object(customer_handler, "upsert_contact"),
			patch.object(customer_handler, "append_business_address_if_present", return_value=None),
			patch.object(customer_handler, "log_step"),
		):
			customer_handler.handle(payload, "corr-no-skip")
		mock_resolver.assert_called_once()

	def test_different_id_does_not_short_circuit(self):
		"""Payload _id != configured common offline id -> normal flow."""
		payload = {"_id": OTHER_CUSTOMER_ID, "updatedAt": "2026-05-20T00:00:00Z"}
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(
				customer_handler,
				"find_or_create_customer",
				return_value=("CUST-Y", True, "wave_id"),
			) as mock_resolver,
			patch.object(customer_handler, "apply_customer_updates"),
			patch.object(customer_handler, "upsert_contact"),
			patch.object(customer_handler, "append_business_address_if_present", return_value=None),
			patch.object(customer_handler, "log_step"),
		):
			customer_handler.handle(payload, "corr-different-id")
		mock_resolver.assert_called_once()
