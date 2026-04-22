"""Controller for the Wave Settings Single DocType."""

import frappe
from frappe import _
from frappe.model.document import Document

INBOUND_API_KEY_LENGTH = 32


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
		self._validate_inbound_api_key_length()

	def _validate_positive_int(self, fieldname: str) -> None:
		"""Refuse zero or negative values for numeric fields that drive math or retention."""
		value = self.get(fieldname) or 0
		if value <= 0:
			frappe.throw(_("{0} must be greater than zero.").format(_(self.meta.get_label(fieldname))))

	def _validate_enabled_requires_keys(self) -> None:
		"""When the integration is enabled, the inbound API key must be set.

		Password fields show a mask string to the UI when untouched. This method
		treats the in-memory value as authoritative: if it is empty the user is
		explicitly clearing the key, and the integration must not remain enabled;
		if it is the mask (all asterisks) the user left the existing value alone
		and we consult the stored password to confirm something is on file.
		"""
		if not self.enabled:
			return
		current = self.inbound_api_key or ""
		if current and all(c == "*" for c in current):
			# Masked value means the stored secret is unchanged; confirm one exists.
			if self.get_password("inbound_api_key", raise_exception=False):
				return
		elif current:
			# User provided a real new key inline; save will persist it.
			return
		frappe.throw(_("Inbound API Key is required when the integration is enabled."))

	def _validate_inbound_api_key_length(self) -> None:
		"""Require the inbound key to be exactly INBOUND_API_KEY_LENGTH characters when a new value is provided.

		A masked value means the user has not touched the field on this save, so
		we skip the length check (the stored value was validated when it was set).
		An empty value is handled by `_validate_enabled_requires_keys`; we do not
		re-raise a second error here.
		"""
		current = self.inbound_api_key or ""
		if not current:
			return
		if all(c == "*" for c in current):
			return
		if len(current) != INBOUND_API_KEY_LENGTH:
			frappe.throw(
				_("Inbound API Key must be exactly {0} characters long (got {1}).").format(
					INBOUND_API_KEY_LENGTH, len(current)
				)
			)
