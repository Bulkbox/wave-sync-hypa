"""Controller for the Wave Settings Single DocType."""

import frappe
from frappe import _
from frappe.model.document import Document


class WaveSettings(Document):
	"""Single config controller.

	Validates cross-field invariants that are easier to express in code than JSON
	(positive integers, non-empty keys when the integration is enabled, etc.).
	"""

	def validate(self):
		"""Run all invariants before the single is saved."""
		self._validate_positive_int("price_scale_divisor")
		self._validate_positive_int("log_retention_days")
		self._validate_enabled_requires_keys()

	def _validate_positive_int(self, fieldname: str) -> None:
		"""Refuse zero or negative values for numeric fields that drive math or retention."""
		value = self.get(fieldname) or 0
		if value <= 0:
			frappe.throw(_("{0} must be greater than zero.").format(_(self.meta.get_label(fieldname))))

	def _validate_enabled_requires_keys(self) -> None:
		"""When the integration is enabled, the inbound API key must be set."""
		if not self.enabled:
			return
		if not self.get_password("inbound_api_key", raise_exception=False):
			frappe.throw(
				_("Inbound API Key is required when the integration is enabled.")
			)
