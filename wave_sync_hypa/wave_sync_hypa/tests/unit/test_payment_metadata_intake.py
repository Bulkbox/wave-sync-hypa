"""Unit tests for handlers.order_create._apply_payment_metadata.

Exercises the payment metadata stamping in isolation: mapping found / missing /
absent paths, cents->major conversion through Wave Settings.price_scale_divisor,
and derivation of wave_payment_state from (classification, paymentStatus).

The function is called as a step in handle() but is decoupled enough to test
directly with a SimpleNamespace-style SO doc + a settings mock. log_step is
patched at the module boundary so no Wave Sync Log rows are written.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers import order_create as oc

DUMMY_WAVE_ID = "wave-id-aaa"
DUMMY_FRIENDLY = "10000050"


def _settings(*, mappings: list[dict] | None = None, divisor: int = 100) -> MagicMock:
	"""Wave Settings stand-in with the payment_method_mappings table + divisor."""
	settings = MagicMock(name="WaveSettings")
	settings.price_scale_divisor = divisor
	values = {"payment_method_mappings": mappings or []}
	settings.get.side_effect = lambda key, default=None: values.get(key, default)
	return settings


def _so() -> SimpleNamespace:
	"""Mutable SO stand-in. _apply_payment_metadata sets attributes on this."""
	return SimpleNamespace(
		wave_payment_classification=None,
		wave_payment_state=None,
		wave_payment_type=None,
		wave_payment_status=None,
		wave_payment_gateway=None,
		wave_payment_reference=None,
		wave_payment_hold=0.0,
		wave_additional_payment_hold=0.0,
		wave_manual_review_required=0,
	)


def _payload(**overrides) -> dict:
	"""Realistic prepaid payload keyed on the live SAL-ORD-2026-00035 example."""
	base = {
		"_id": DUMMY_WAVE_ID,
		"friendlyId": DUMMY_FRIENDLY,
		"paymentType": "card",
		"paymentStatus": "COMPLETED",
		"paymentGateway": "IPAYAFRICA",
		"paymentReference": "10000050T1778145927615",
		"paymentHold": 22000,
		"additionalPaymentHold": {"type": "ABSOLUTE", "value": 0, "amount": 0},
	}
	base.update(overrides)
	return base


def _mapping(payment_type: str, classification: str, mode: str | None = None) -> dict:
	return {
		"wave_payment_type": payment_type,
		"classification": classification,
		"mode_of_payment": mode,
	}


class TestApplyPaymentMetadata(FrappeTestCase):
	"""Stamping logic: cover all three branches plus state derivation."""

	def test_prepaid_card_completed_stamps_paid_online(self):
		so = _so()
		settings = _settings(mappings=[_mapping("card", "prepaid", "Wave Card")])
		with patch.object(oc, "log_step") as mock_log:
			oc._apply_payment_metadata(so, settings, _payload(), "corr-1")
		self.assertEqual(so.wave_payment_classification, "prepaid")
		self.assertEqual(so.wave_payment_state, "Paid (Online)")
		self.assertEqual(so.wave_payment_type, "card")
		self.assertEqual(so.wave_payment_status, "COMPLETED")
		self.assertEqual(so.wave_payment_gateway, "IPAYAFRICA")
		self.assertEqual(so.wave_payment_reference, "10000050T1778145927615")
		self.assertEqual(so.wave_payment_hold, 220.00)  # 22000 / 100
		self.assertEqual(so.wave_additional_payment_hold, 0.0)
		# COMPLETED prepaid is the happy path; no manual review.
		self.assertFalse(so.wave_manual_review_required)
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertIn(oc.STEP_PAYMENT_METADATA_STAMPED, steps)

	def test_prepaid_pending_status_flags_manual_review(self):
		so = _so()
		settings = _settings(mappings=[_mapping("card", "prepaid", "Wave Card")])
		with patch.object(oc, "log_step"):
			oc._apply_payment_metadata(so, settings, _payload(paymentStatus="PENDING"), "corr-2")
		self.assertEqual(so.wave_payment_state, "Pending")
		self.assertTrue(so.wave_manual_review_required)

	def test_prepaid_failed_status_maps_to_failed_state(self):
		so = _so()
		settings = _settings(mappings=[_mapping("card", "prepaid", "Wave Card")])
		with patch.object(oc, "log_step"):
			oc._apply_payment_metadata(so, settings, _payload(paymentStatus="FAILED"), "corr-3")
		self.assertEqual(so.wave_payment_state, "Failed")
		self.assertTrue(so.wave_manual_review_required)

	def test_prepaid_cancelled_status_maps_to_refunded_state(self):
		so = _so()
		settings = _settings(mappings=[_mapping("card", "prepaid", "Wave Card")])
		with patch.object(oc, "log_step"):
			oc._apply_payment_metadata(so, settings, _payload(paymentStatus="CANCELLED"), "corr-4")
		self.assertEqual(so.wave_payment_state, "Refunded")
		self.assertTrue(so.wave_manual_review_required)

	def test_cod_card_on_delivery_stamps_awaiting(self):
		so = _so()
		settings = _settings(mappings=[_mapping("cardOnDelivery", "cod", "Cash")])
		payload = _payload(
			paymentType="cardOnDelivery",
			paymentStatus="",
			paymentGateway="",
			paymentReference="",
			paymentHold=0,
			additionalPaymentHold={"amount": 0},
		)
		with patch.object(oc, "log_step"):
			oc._apply_payment_metadata(so, settings, payload, "corr-5")
		self.assertEqual(so.wave_payment_classification, "cod")
		self.assertEqual(so.wave_payment_state, "Awaiting Cash on Delivery")
		# COD doesn't carry a payment reference; we tolerate empty values.
		self.assertIsNone(so.wave_payment_reference)
		self.assertFalse(so.wave_manual_review_required)

	def test_cod_cash_classification(self):
		so = _so()
		settings = _settings(mappings=[_mapping("cash", "cod")])
		payload = _payload(paymentType="cash", paymentHold=0)
		with patch.object(oc, "log_step"):
			oc._apply_payment_metadata(so, settings, payload, "corr-6")
		self.assertEqual(so.wave_payment_classification, "cod")
		self.assertEqual(so.wave_payment_state, "Awaiting Cash on Delivery")

	def test_missing_mapping_stamps_raw_fields_and_flags_manual_review(self):
		so = _so()
		# Empty mapping table.
		settings = _settings(mappings=[])
		with patch.object(oc, "log_step") as mock_log:
			oc._apply_payment_metadata(so, settings, _payload(paymentType="newGatewayType"), "corr-7")
		# Raw stamping happened so operators can investigate.
		self.assertEqual(so.wave_payment_type, "newGatewayType")
		self.assertEqual(so.wave_payment_hold, 220.00)
		# But classification + state are blank because we don't know the class.
		self.assertIsNone(so.wave_payment_classification)
		self.assertIsNone(so.wave_payment_state)
		self.assertTrue(so.wave_manual_review_required)
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertIn(oc.STEP_PAYMENT_MAPPING_MISSING, steps)

	def test_payment_type_absent_logs_and_no_ops(self):
		so = _so()
		settings = _settings(mappings=[])
		payload = {"_id": DUMMY_WAVE_ID, "friendlyId": DUMMY_FRIENDLY}
		with patch.object(oc, "log_step") as mock_log:
			oc._apply_payment_metadata(so, settings, payload, "corr-8")
		# Nothing was stamped.
		self.assertIsNone(so.wave_payment_type)
		self.assertEqual(so.wave_payment_hold, 0.0)
		self.assertFalse(so.wave_manual_review_required)
		steps = [c.args[1] for c in mock_log.call_args_list]
		self.assertEqual(steps, [oc.STEP_PAYMENT_METADATA_ABSENT])

	def test_cents_to_major_uses_settings_divisor_not_a_constant(self):
		so = _so()
		# divisor=1000 sanity-checks that the field is honoured; nothing in the
		# stamping path silently falls back to /100.
		settings = _settings(mappings=[_mapping("card", "prepaid")], divisor=1000)
		with patch.object(oc, "log_step"):
			oc._apply_payment_metadata(so, settings, _payload(), "corr-9")
		self.assertEqual(so.wave_payment_hold, 22.00)  # 22000 / 1000

	def test_additional_payment_hold_summed_when_nonzero(self):
		so = _so()
		settings = _settings(mappings=[_mapping("card", "prepaid")])
		payload = _payload(
			paymentHold=20000,
			additionalPaymentHold={"type": "ABSOLUTE", "value": 200, "amount": 200},
		)
		with patch.object(oc, "log_step"):
			oc._apply_payment_metadata(so, settings, payload, "corr-10")
		self.assertEqual(so.wave_payment_hold, 200.00)
		self.assertEqual(so.wave_additional_payment_hold, 2.00)

	def test_mapping_with_multiple_rows_picks_first_match(self):
		so = _so()
		# Operators typically have one row per paymentType, but the resolver
		# should still be deterministic if duplicates exist.
		settings = _settings(mappings=[
			_mapping("klarna", "prepaid"),
			_mapping("card", "prepaid", "Wave Card"),
			_mapping("cash", "cod"),
		])
		with patch.object(oc, "log_step"):
			oc._apply_payment_metadata(so, settings, _payload(), "corr-11")
		self.assertEqual(so.wave_payment_classification, "prepaid")
