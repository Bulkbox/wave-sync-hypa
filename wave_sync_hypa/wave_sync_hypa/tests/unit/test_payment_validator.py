"""Unit tests for services.payment_validator.validate_pe_before_submit.

Covers the seven branches:
  * pass-through (no Wave references)
  * mixed prepaid + COD -> hard block
  * prepaid PE without Sales Invoice ref -> hard block
  * prepaid PE amount mismatch -> hard block
  * prepaid PE MOP mismatch -> Warning, no raise
  * COD PE with non-COD MOP -> hard block
  * override role bypasses hard blocks with a Warning row

`frappe.db.get_value`, `frappe.db.get_all`, `frappe.get_cached_doc`,
`frappe.session`, `frappe.get_roles`, and the `log_step` audit call are all
mocked at module boundaries so the validator is exercised in pure unit form.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.services import payment_validator as pv

DUMMY_PE = "PE-2026-0001"
WAVE_ORDER_ID_A = "wave-aaa"
WAVE_ORDER_ID_B = "wave-bbb"
SO_A = "SAL-ORD-2026-0001"
SO_B = "SAL-ORD-2026-0002"
SI_A = "SI-2026-0001"


def _ref(doctype: str, name: str) -> dict:
	return {"reference_doctype": doctype, "reference_name": name}


def _pe(*, references=None, paid_amount=0.0, mode_of_payment="MPESA") -> SimpleNamespace:
	"""Lightweight PE doc stand-in. validate_pe_before_submit reads these via .get()."""
	doc = SimpleNamespace()
	doc.doctype = "Payment Entry"
	doc.name = DUMMY_PE
	values = {
		"references": references or [],
		"paid_amount": paid_amount,
		"mode_of_payment": mode_of_payment,
		"wave_correlation_id": "corr-test",
	}
	doc.get = lambda key, default=None: values.get(key, default)
	return doc


def _settings(*, mappings: list[dict] | None = None) -> MagicMock:
	"""Wave Settings stand-in for _expected_mop / _classify_mode_of_payment lookups."""
	settings = MagicMock(name="WaveSettings")
	settings.get.side_effect = lambda key, default=None: {
		"payment_method_mappings": mappings or [],
	}.get(key, default)
	return settings


def _so_metadata(
	*,
	classification: str = "prepaid",
	payment_type: str = "card",
	hold: float = 220.00,
	additional: float = 0.0,
	friendly: str = "10000050",
) -> dict:
	return {
		"wave_payment_classification": classification,
		"wave_payment_type": payment_type,
		"wave_payment_hold": hold,
		"wave_additional_payment_hold": additional,
		"wave_friendly_id": friendly,
	}


class TestPaymentValidator(FrappeTestCase):
	"""All seven branches of validate_pe_before_submit."""

	def test_pass_through_when_no_wave_references(self):
		pe = _pe(references=[_ref("Journal Entry", "JE-1")], paid_amount=100)
		# No Wave-sourced refs at all -> never even reads settings.
		with (
			patch.object(frappe.db, "get_value", return_value=""),  # no wave_order_id
			patch.object(pv, "log_step") as mock_log,
		):
			pv.validate_pe_before_submit(pe)
		mock_log.assert_not_called()

	def test_pass_through_with_empty_references(self):
		"""The n8n unallocated 'Ipay Unallocated' PE has references=[] — must pass."""
		pe = _pe(references=[], paid_amount=220)
		with patch.object(pv, "log_step") as mock_log:
			pv.validate_pe_before_submit(pe)
		mock_log.assert_not_called()

	def test_mixed_prepaid_and_cod_blocks_with_clear_message(self):
		pe = _pe(
			references=[_ref("Sales Order", SO_A), _ref("Sales Order", SO_B)],
			paid_amount=440,
			mode_of_payment="MPESA",
		)
		settings = _settings(mappings=[
			{"wave_payment_type": "card", "classification": "prepaid", "mode_of_payment": "Wave Card"},
			{"wave_payment_type": "cash", "classification": "cod", "mode_of_payment": "Cash"},
		])

		def get_value(doctype, name, *args, **kwargs):
			if doctype == "Sales Order" and isinstance(args[0], str) and args[0] == "wave_order_id":
				return {SO_A: WAVE_ORDER_ID_A, SO_B: WAVE_ORDER_ID_B}.get(name, "")
			return ""

		def get_value_dict(doctype, name, fields, as_dict=True):
			return {
				SO_A: _so_metadata(classification="prepaid", payment_type="card"),
				SO_B: _so_metadata(classification="cod", payment_type="cash"),
			}.get(name)

		def get_value_dispatch(doctype, name, fields, as_dict=False):
			if isinstance(fields, list):
				return get_value_dict(doctype, name, fields, as_dict=as_dict)
			# wave_order_id lookup
			return {SO_A: WAVE_ORDER_ID_A, SO_B: WAVE_ORDER_ID_B}.get(name, "")

		with (
			patch.object(frappe.db, "get_value", side_effect=get_value_dispatch),
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe, "session", MagicMock(user="acct@example.com")),
			patch.object(frappe, "get_roles", return_value=["Accounts User"]),
			patch.object(pv, "log_step") as mock_log,
		):
			with self.assertRaises(frappe.ValidationError):
				pv.validate_pe_before_submit(pe)
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pv.STEP_BLOCKED_MIXED_CLASS, steps)

	def test_prepaid_so_only_no_si_blocks(self):
		"""Prepaid PE referencing SO without an SI must be blocked."""
		pe = _pe(references=[_ref("Sales Order", SO_A)], paid_amount=220, mode_of_payment="Wave Card")
		settings = _settings(mappings=[
			{"wave_payment_type": "card", "classification": "prepaid", "mode_of_payment": "Wave Card"},
		])

		def get_value_dispatch(doctype, name, fields, as_dict=False):
			if isinstance(fields, list):
				return _so_metadata(classification="prepaid", payment_type="card")
			return WAVE_ORDER_ID_A

		with (
			patch.object(frappe.db, "get_value", side_effect=get_value_dispatch),
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe, "session", MagicMock(user="acct@example.com")),
			patch.object(frappe, "get_roles", return_value=["Accounts User"]),
			patch.object(pv, "log_step") as mock_log,
		):
			with self.assertRaises(frappe.ValidationError):
				pv.validate_pe_before_submit(pe)
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pv.STEP_BLOCKED_PREPAID_NO_SI, steps)

	def test_prepaid_with_si_and_matching_amount_passes(self):
		pe = _pe(
			references=[_ref("Sales Order", SO_A), _ref("Sales Invoice", SI_A)],
			paid_amount=220.00,
			mode_of_payment="Wave Card",
		)
		settings = _settings(mappings=[
			{"wave_payment_type": "card", "classification": "prepaid", "mode_of_payment": "Wave Card"},
		])

		def get_value_dispatch(doctype, name, fields, as_dict=False):
			if isinstance(fields, list):
				return _so_metadata(classification="prepaid", payment_type="card")
			# wave_order_id; both SO and SI map to WAVE_ORDER_ID_A
			return WAVE_ORDER_ID_A

		def get_all_dispatch(doctype, filters=None, fields=None, **kwargs):
			# SI's items[] -> sales_order resolution
			if doctype == "Sales Invoice Item":
				return [{"sales_order": SO_A}]
			return []

		with (
			patch.object(frappe.db, "get_value", side_effect=get_value_dispatch),
			patch.object(frappe.db, "get_all", side_effect=get_all_dispatch),
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe, "session", MagicMock(user="acct@example.com")),
			patch.object(frappe, "get_roles", return_value=["Accounts User"]),
			patch.object(pv, "log_step") as mock_log,
		):
			pv.validate_pe_before_submit(pe)
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pv.STEP_VALIDATED, steps)
		self.assertNotIn(pv.STEP_WARN_MOP_MISMATCH, steps)

	def test_prepaid_amount_mismatch_blocks(self):
		pe = _pe(
			references=[_ref("Sales Order", SO_A), _ref("Sales Invoice", SI_A)],
			paid_amount=99.00,  # wrong; expected 220
			mode_of_payment="Wave Card",
		)
		settings = _settings(mappings=[
			{"wave_payment_type": "card", "classification": "prepaid", "mode_of_payment": "Wave Card"},
		])

		def get_value_dispatch(doctype, name, fields, as_dict=False):
			if isinstance(fields, list):
				return _so_metadata(hold=220.00)
			return WAVE_ORDER_ID_A

		with (
			patch.object(frappe.db, "get_value", side_effect=get_value_dispatch),
			patch.object(frappe.db, "get_all", return_value=[{"sales_order": SO_A}]),
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe, "session", MagicMock(user="acct@example.com")),
			patch.object(frappe, "get_roles", return_value=["Accounts User"]),
			patch.object(pv, "log_step") as mock_log,
		):
			with self.assertRaises(frappe.ValidationError):
				pv.validate_pe_before_submit(pe)
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pv.STEP_BLOCKED_PREPAID_AMOUNT, steps)

	def test_prepaid_mop_mismatch_warns_only(self):
		"""n8n hardcodes MPESA; MOP mismatch must be Warning, not block."""
		pe = _pe(
			references=[_ref("Sales Order", SO_A), _ref("Sales Invoice", SI_A)],
			paid_amount=220.00,
			mode_of_payment="MPESA",  # mapping says Wave Card
		)
		settings = _settings(mappings=[
			{"wave_payment_type": "card", "classification": "prepaid", "mode_of_payment": "Wave Card"},
		])

		def get_value_dispatch(doctype, name, fields, as_dict=False):
			if isinstance(fields, list):
				return _so_metadata()
			return WAVE_ORDER_ID_A

		with (
			patch.object(frappe.db, "get_value", side_effect=get_value_dispatch),
			patch.object(frappe.db, "get_all", return_value=[{"sales_order": SO_A}]),
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe, "session", MagicMock(user="acct@example.com")),
			patch.object(frappe, "get_roles", return_value=["Accounts User"]),
			patch.object(pv, "log_step") as mock_log,
		):
			pv.validate_pe_before_submit(pe)  # must NOT raise
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pv.STEP_WARN_MOP_MISMATCH, steps)
		self.assertIn(pv.STEP_VALIDATED, steps)

	def test_prepaid_mop_mismatch_skipped_when_mapping_mop_blank(self):
		"""Operator hasn't filled in MOP yet — no warning, no block."""
		pe = _pe(
			references=[_ref("Sales Order", SO_A), _ref("Sales Invoice", SI_A)],
			paid_amount=220.00,
			mode_of_payment="MPESA",
		)
		settings = _settings(mappings=[
			# mode_of_payment intentionally blank.
			{"wave_payment_type": "card", "classification": "prepaid", "mode_of_payment": ""},
		])

		def get_value_dispatch(doctype, name, fields, as_dict=False):
			if isinstance(fields, list):
				return _so_metadata()
			return WAVE_ORDER_ID_A

		with (
			patch.object(frappe.db, "get_value", side_effect=get_value_dispatch),
			patch.object(frappe.db, "get_all", return_value=[{"sales_order": SO_A}]),
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe, "session", MagicMock(user="acct@example.com")),
			patch.object(frappe, "get_roles", return_value=["Accounts User"]),
			patch.object(pv, "log_step") as mock_log,
		):
			pv.validate_pe_before_submit(pe)
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertNotIn(pv.STEP_WARN_MOP_MISMATCH, steps)

	def test_cod_with_non_cod_mop_blocks(self):
		pe = _pe(
			references=[_ref("Sales Order", SO_A)],
			paid_amount=220.00,
			mode_of_payment="Wave Card",  # prepaid-classified
		)
		settings = _settings(mappings=[
			{"wave_payment_type": "card", "classification": "prepaid", "mode_of_payment": "Wave Card"},
			{"wave_payment_type": "cash", "classification": "cod", "mode_of_payment": "Cash"},
		])

		def get_value_dispatch(doctype, name, fields, as_dict=False):
			if isinstance(fields, list):
				return _so_metadata(classification="cod", payment_type="cash")
			return WAVE_ORDER_ID_A

		with (
			patch.object(frappe.db, "get_value", side_effect=get_value_dispatch),
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe, "session", MagicMock(user="acct@example.com")),
			patch.object(frappe, "get_roles", return_value=["Accounts User"]),
			patch.object(pv, "log_step") as mock_log,
		):
			with self.assertRaises(frappe.ValidationError):
				pv.validate_pe_before_submit(pe)
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pv.STEP_BLOCKED_COD_MOP, steps)

	def test_cod_with_cod_mop_passes(self):
		pe = _pe(
			references=[_ref("Sales Order", SO_A)],
			paid_amount=220.00,
			mode_of_payment="Cash",
		)
		settings = _settings(mappings=[
			{"wave_payment_type": "cash", "classification": "cod", "mode_of_payment": "Cash"},
		])

		def get_value_dispatch(doctype, name, fields, as_dict=False):
			if isinstance(fields, list):
				return _so_metadata(classification="cod", payment_type="cash")
			return WAVE_ORDER_ID_A

		with (
			patch.object(frappe.db, "get_value", side_effect=get_value_dispatch),
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe, "session", MagicMock(user="acct@example.com")),
			patch.object(frappe, "get_roles", return_value=["Accounts User"]),
			patch.object(pv, "log_step") as mock_log,
		):
			pv.validate_pe_before_submit(pe)
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pv.STEP_VALIDATED, steps)

	def test_cod_zero_amount_blocks(self):
		pe = _pe(
			references=[_ref("Sales Order", SO_A)],
			paid_amount=0.0,
			mode_of_payment="Cash",
		)
		settings = _settings(mappings=[
			{"wave_payment_type": "cash", "classification": "cod", "mode_of_payment": "Cash"},
		])

		def get_value_dispatch(doctype, name, fields, as_dict=False):
			if isinstance(fields, list):
				return _so_metadata(classification="cod", payment_type="cash")
			return WAVE_ORDER_ID_A

		with (
			patch.object(frappe.db, "get_value", side_effect=get_value_dispatch),
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe, "session", MagicMock(user="acct@example.com")),
			patch.object(frappe, "get_roles", return_value=["Accounts User"]),
			patch.object(pv, "log_step") as mock_log,
		):
			with self.assertRaises(frappe.ValidationError):
				pv.validate_pe_before_submit(pe)
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pv.STEP_BLOCKED_COD_AMOUNT, steps)

	def test_override_role_bypasses_amount_block_with_warning(self):
		pe = _pe(
			references=[_ref("Sales Order", SO_A), _ref("Sales Invoice", SI_A)],
			paid_amount=99.00,  # would normally block
			mode_of_payment="Wave Card",
		)
		settings = _settings(mappings=[
			{"wave_payment_type": "card", "classification": "prepaid", "mode_of_payment": "Wave Card"},
		])

		def get_value_dispatch(doctype, name, fields, as_dict=False):
			if isinstance(fields, list):
				return _so_metadata(hold=220.00)
			return WAVE_ORDER_ID_A

		with (
			patch.object(frappe.db, "get_value", side_effect=get_value_dispatch),
			patch.object(frappe.db, "get_all", return_value=[{"sales_order": SO_A}]),
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe, "session", MagicMock(user="ops@example.com")),
			patch.object(frappe, "get_roles", return_value=["Accounts User", pv.OVERRIDE_ROLE]),
			patch.object(pv, "log_step") as mock_log,
		):
			pv.validate_pe_before_submit(pe)  # must NOT raise
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pv.STEP_OVERRIDDEN, steps)
		# The blocked step must NOT have been written (override took the
		# alternate branch).
		self.assertNotIn(pv.STEP_BLOCKED_PREPAID_AMOUNT, steps)

	def test_system_manager_bypasses_blocks(self):
		"""System Manager is the never-lockout fallback even for the validator."""
		pe = _pe(
			references=[_ref("Sales Order", SO_A)],  # no SI -> would block
			paid_amount=220.00,
			mode_of_payment="Wave Card",
		)
		settings = _settings(mappings=[
			{"wave_payment_type": "card", "classification": "prepaid", "mode_of_payment": "Wave Card"},
		])

		def get_value_dispatch(doctype, name, fields, as_dict=False):
			if isinstance(fields, list):
				return _so_metadata()
			return WAVE_ORDER_ID_A

		with (
			patch.object(frappe.db, "get_value", side_effect=get_value_dispatch),
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(frappe, "session", MagicMock(user="admin@example.com")),
			patch.object(frappe, "get_roles", return_value=["System Manager"]),
			patch.object(pv, "log_step") as mock_log,
		):
			pv.validate_pe_before_submit(pe)
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pv.STEP_OVERRIDDEN, steps)
