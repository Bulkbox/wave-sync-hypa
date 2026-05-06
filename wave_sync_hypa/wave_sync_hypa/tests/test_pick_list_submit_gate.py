"""Unit tests for the Pick List submit/cancel lockdown gate.

Covers all eight branches of `_enforce_pick_list_action_gate`:

  1. lockdown OFF -> no-op (default behaviour, existing sites unaffected)
  2. inbound webhook flag set -> bypass (forward-compat seam for the future
     pick-list-submit-inbound webhook handler)
  3. user has the dedicated override role -> pass
  4. user is System Manager -> pass (always; never a lockout)
  5. unprivileged user, lockdown on, submit -> Warning row + PermissionError
  6. unprivileged user, lockdown on, cancel -> Warning row + PermissionError
  7. blocked attempt writes a Wave Sync Log row tagged with user + doc
  8. inbound flag bypass works for cancel as well as submit

`frappe.session`, `frappe.get_roles`, `frappe.get_cached_doc`, and the
`log_step` audit call are patched at the module boundary so the gate is
exercised in pure unit form (no DB roundtrips, no real role lookups).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers import pick_list as pl_handler

DUMMY_PL = "PICK-2026-0042"
DUMMY_WAVE_ORDER_ID = "wave-id-zzz"
WAREHOUSE_USER = "warehouse@example.com"
OPS_USER = "ops@example.com"
SYSADMIN_USER = "admin@example.com"


def _settings(*, lockdown_on: bool = True) -> MagicMock:
	"""Wave Settings stand-in: only the lockdown switch matters here."""
	values = {"pick_list_erp_submit_lockdown_enabled": 1 if lockdown_on else 0}
	settings = MagicMock(name="WaveSettings")
	settings.get.side_effect = lambda key, default=None: values.get(key, default)
	return settings


def _doc(wave_order_id: str = DUMMY_WAVE_ORDER_ID) -> MagicMock:
	"""Pick List doc stand-in: only `name`, `doctype`, and a single `wave_order_id` are read."""
	doc = MagicMock(name="PickListDoc")
	doc.name = DUMMY_PL
	doc.doctype = "Pick List"
	doc.get.side_effect = lambda key, default=None: {
		"wave_order_id": wave_order_id,
	}.get(key, default)
	return doc


class TestPickListSubmitGate(FrappeTestCase):
	"""Submit/cancel guard: pass when allowed, raise + log when blocked."""

	def test_lockdown_off_lets_anyone_submit(self):
		"""Existing sites with lockdown off see no behaviour change."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(lockdown_on=False)),
			patch.object(frappe, "session", MagicMock(user=WAREHOUSE_USER)),
			patch.object(frappe, "get_roles", return_value=["Stock User"]),
			patch.object(pl_handler, "log_step") as mock_log,
		):
			pl_handler.block_unprivileged_pick_list_submit(_doc())
		# Pass-through: no audit row, no exception.
		mock_log.assert_not_called()

	def test_lockdown_off_lets_anyone_cancel(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(lockdown_on=False)),
			patch.object(frappe, "session", MagicMock(user=WAREHOUSE_USER)),
			patch.object(frappe, "get_roles", return_value=["Stock User"]),
			patch.object(pl_handler, "log_step") as mock_log,
		):
			pl_handler.block_unprivileged_pick_list_cancel(_doc())
		mock_log.assert_not_called()

	def test_lockdown_on_blocks_unprivileged_submit_with_permission_error(self):
		"""Stock User attempting to submit -> PermissionError raised, Warning row written."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "session", MagicMock(user=WAREHOUSE_USER)),
			patch.object(frappe, "get_roles", return_value=["Stock User"]),
			patch.object(pl_handler, "log_step") as mock_log,
		):
			with self.assertRaises(frappe.PermissionError):
				pl_handler.block_unprivileged_pick_list_submit(_doc())
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pl_handler.STEP_SUBMIT_BLOCKED, steps)

	def test_lockdown_on_blocks_unprivileged_cancel_with_permission_error(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "session", MagicMock(user=WAREHOUSE_USER)),
			patch.object(frappe, "get_roles", return_value=["Stock User"]),
			patch.object(pl_handler, "log_step") as mock_log,
		):
			with self.assertRaises(frappe.PermissionError):
				pl_handler.block_unprivileged_pick_list_cancel(_doc())
		steps = [c.kwargs.get("step") for c in mock_log.call_args_list]
		self.assertIn(pl_handler.STEP_CANCEL_BLOCKED, steps)

	def test_user_with_override_role_can_submit_when_lockdown_on(self):
		"""Granting the dedicated role lifts the gate without touching any other perm."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "session", MagicMock(user=OPS_USER)),
			patch.object(
				frappe, "get_roles",
				return_value=["Stock User", pl_handler.PICK_LIST_OVERRIDE_ROLE],
			),
			patch.object(pl_handler, "log_step") as mock_log,
		):
			pl_handler.block_unprivileged_pick_list_submit(_doc())
		mock_log.assert_not_called()

	def test_user_with_override_role_can_cancel_when_lockdown_on(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "session", MagicMock(user=OPS_USER)),
			patch.object(
				frappe, "get_roles",
				return_value=["Stock User", pl_handler.PICK_LIST_OVERRIDE_ROLE],
			),
			patch.object(pl_handler, "log_step") as mock_log,
		):
			pl_handler.block_unprivileged_pick_list_cancel(_doc())
		mock_log.assert_not_called()

	def test_system_manager_always_allowed_when_lockdown_on(self):
		"""System Manager is the never-lockout fallback, even without the override role."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "session", MagicMock(user=SYSADMIN_USER)),
			patch.object(frappe, "get_roles", return_value=["System Manager"]),
			patch.object(pl_handler, "log_step") as mock_log,
		):
			pl_handler.block_unprivileged_pick_list_submit(_doc())
			pl_handler.block_unprivileged_pick_list_cancel(_doc())
		mock_log.assert_not_called()

	def test_inbound_flag_bypasses_submit_gate(self):
		"""Documents the forward-compat seam for the future webhook handler."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "session", MagicMock(user=WAREHOUSE_USER)),
			patch.object(frappe, "get_roles", return_value=["Stock User"]),
			patch.object(pl_handler, "log_step") as mock_log,
		):
			frappe.flags[pl_handler.INBOUND_SUBMIT_FLAG] = True
			try:
				pl_handler.block_unprivileged_pick_list_submit(_doc())
			finally:
				frappe.flags.pop(pl_handler.INBOUND_SUBMIT_FLAG, None)
		mock_log.assert_not_called()

	def test_inbound_flag_bypasses_cancel_gate(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "session", MagicMock(user=WAREHOUSE_USER)),
			patch.object(frappe, "get_roles", return_value=["Stock User"]),
			patch.object(pl_handler, "log_step") as mock_log,
		):
			frappe.flags[pl_handler.INBOUND_SUBMIT_FLAG] = True
			try:
				pl_handler.block_unprivileged_pick_list_cancel(_doc())
			finally:
				frappe.flags.pop(pl_handler.INBOUND_SUBMIT_FLAG, None)
		mock_log.assert_not_called()

	def test_blocked_attempt_writes_warning_row_with_user_and_doc(self):
		"""Audit row should capture who attempted what so ops can spot training gaps."""
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(frappe, "session", MagicMock(user=WAREHOUSE_USER)),
			patch.object(frappe, "get_roles", return_value=["Stock User"]),
			patch.object(pl_handler, "log_step") as mock_log,
		):
			with self.assertRaises(frappe.PermissionError):
				pl_handler.block_unprivileged_pick_list_submit(_doc())
		# Exactly one Warning row, tagged with the right step.
		self.assertEqual(len(mock_log.call_args_list), 1)
		call = mock_log.call_args_list[0]
		self.assertEqual(call.kwargs.get("step"), pl_handler.STEP_SUBMIT_BLOCKED)
		self.assertEqual(call.kwargs.get("level"), "Warning")
		self.assertEqual(call.kwargs.get("linked_docname"), DUMMY_PL)
		self.assertEqual(call.kwargs.get("wave_id"), DUMMY_WAVE_ORDER_ID)
		# The error message should mention the user + the override role so
		# operators reading the row know what to do next.
		err = call.kwargs.get("error_message") or ""
		self.assertIn(WAREHOUSE_USER, err)
		self.assertIn(pl_handler.PICK_LIST_OVERRIDE_ROLE, err)
