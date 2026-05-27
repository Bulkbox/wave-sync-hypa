"""Unit tests for handlers.sales_order_amend.wipe_wave_fields_on_amend.

Frappe's amend ignores no_copy by design, so we wipe wave_* fields ourselves
on the amended copy of a Sales Order. These tests pin the scope (SO only),
the amend-only trigger, and the conditional po_no clear.
"""

from __future__ import annotations

from types import SimpleNamespace

from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers.sales_order_amend import (
	_WAVE_FIELDS_TO_WIPE_ON_AMEND,
	wipe_wave_fields_on_amend,
)


def _so_doc(
	*,
	amended_from: str | None = "SAL-ORD-OLD",
	po_no: str | None = None,
	**field_overrides,
) -> SimpleNamespace:
	"""SO stand-in carrying doctype + amended_from + every wave_* field PRE-populated.

	Override any specific field via kwargs (e.g. wave_friendly_id="10000099").
	"""
	doc = SimpleNamespace(doctype="Sales Order", amended_from=amended_from, po_no=po_no)
	for field in _WAVE_FIELDS_TO_WIPE_ON_AMEND:
		setattr(doc, field, field_overrides.pop(field, f"PRE-{field}"))
	for k, v in field_overrides.items():
		setattr(doc, k, v)

	def _get(key, default=None):
		return getattr(doc, key, default)

	def _set(key, value):
		setattr(doc, key, value)

	doc.get = _get
	doc.set = _set
	return doc


class TestWipeWaveFieldsOnAmend(FrappeTestCase):
	"""Scope: Sales Order only. Trigger: amended_from is set. Behaviour: blank wave_* + conditional po_no."""

	def test_wipes_all_wave_fields_on_amended_sales_order(self):
		"""Amended SO with every wave_* field populated -> all blanked to None."""
		doc = _so_doc()
		wipe_wave_fields_on_amend(doc)
		for field in _WAVE_FIELDS_TO_WIPE_ON_AMEND:
			self.assertIsNone(getattr(doc, field), f"{field} should be None after wipe")

	def test_no_op_when_amended_from_is_blank(self):
		"""Fresh SO insert (no amend) -> wave_* fields preserved."""
		doc = _so_doc(amended_from=None)
		wipe_wave_fields_on_amend(doc)
		for field in _WAVE_FIELDS_TO_WIPE_ON_AMEND:
			self.assertEqual(getattr(doc, field), f"PRE-{field}", f"{field} should be untouched")

	def test_no_op_when_doctype_is_not_sales_order(self):
		"""Defensive in-function scope guard: DN amend doesn't wipe wave_order_id."""
		dn = _so_doc()
		dn.doctype = "Delivery Note"
		wipe_wave_fields_on_amend(dn)
		# wave_order_id (and every other wave_* field) stays populated.
		for field in _WAVE_FIELDS_TO_WIPE_ON_AMEND:
			self.assertEqual(getattr(dn, field), f"PRE-{field}", f"{field} touched on non-SO doctype")

	def test_clears_po_no_when_it_matches_wave_friendly_id(self):
		"""po_no = friendly id (we stamped it) -> cleared along with wave_* fields."""
		doc = _so_doc(wave_friendly_id="10000099", po_no="10000099")
		wipe_wave_fields_on_amend(doc)
		self.assertIsNone(doc.po_no)

	def test_preserves_operator_set_po_no(self):
		"""po_no doesn't match friendly id (operator paper PO) -> preserved."""
		doc = _so_doc(wave_friendly_id="10000099", po_no="OPERATOR-PO-123")
		wipe_wave_fields_on_amend(doc)
		self.assertEqual(doc.po_no, "OPERATOR-PO-123")
