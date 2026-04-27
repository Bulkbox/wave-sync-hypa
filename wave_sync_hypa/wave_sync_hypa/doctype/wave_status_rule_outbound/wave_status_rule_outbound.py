"""Controller for Wave Status Rule Outbound (child of Wave Settings)."""

import frappe
from frappe import _
from frappe.model.document import Document


class WaveStatusRuleOutbound(Document):
	"""Row-level controller; enforces 'at least one Wave field set' and condition-field invariants."""

	def validate(self):
		"""Reject rows that would never produce a usable PUT body or never match anything."""
		self._require_at_least_one_wave_field()
		self._require_value_when_condition_field_set()

	def _require_at_least_one_wave_field(self) -> None:
		"""A rule that sets neither status nor deliveryStatus contributes nothing to the PUT body."""
		if not self.wave_status and not self.wave_delivery_status:
			frappe.throw(
				_(
					"Outbound Status Rule must set Wave Status or Wave Delivery Status (or both)."
				)
			)

	def _require_value_when_condition_field_set(self) -> None:
		"""A condition field without a value would silently match everything; demand both or neither."""
		has_field = bool((self.erp_condition_field or "").strip())
		has_value = bool((self.erp_condition_value or "").strip())
		if has_field != has_value:
			frappe.throw(
				_(
					"Set both Condition Field and Condition Value, or leave both blank."
				)
			)
