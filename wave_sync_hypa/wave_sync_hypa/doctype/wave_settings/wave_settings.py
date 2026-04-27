"""Controller for the Wave Settings Single DocType.

Three concerns lumped here on purpose because they share lifecycle state:
  1. validate() — invariants that block bad saves.
  2. on_update() — audit snapshot of child-table counts after every save,
     so any future regression that wipes rules / mappings shows up in the
     Wave Sync Log immediately instead of being noticed days later.
  3. Tolerance for framework-driven writes — install / migrate must not
     trip operator-facing validations.
"""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.model.document import Document

INBOUND_API_KEY_LENGTH = 32
INBOUND_API_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

CHILD_TABLE_FIELDS = (
	"route_rules",
	"inbound_status_rules",
	"outbound_status_rules",
	"fee_mappings",
	"tax_rules",
)

PASSWORD_FIELDS = ("inbound_api_key", "wave_api_key")


class WaveSettings(Document):
	"""Single config controller.

	Tolerant of every shape Frappe presents the in-memory doc in across
	form save, install, migrate, and patch flows. Operator-facing validation
	throws only when persisting the save would actually leave the integration
	in a broken state — never on cosmetic round-trips of masked / null /
	empty fields when the encrypted store already holds a valid value.
	"""

	def before_save(self):
		"""Restore Password fields the form did not actually change so __Auth is not wiped.

		Root cause of the data-loss reports: Frappe's save pipeline treats an
		in-memory Password field of None / "" as "operator cleared the secret"
		and DELETEs the encrypted row from __Auth. Browsers, child-table inline
		edits, and partial form posts routinely send the field as None, even
		when the operator never touched it. Without this guard, every such
		save silently wipes the inbound key (and downstream the outbound key,
		since both are Password fields).

		Fix: when the in-memory value is empty / None / fully masked AND the
		encrypted store still has a value, copy the cleartext back into the
		in-memory field. Frappe then re-encrypts the same value on save, sees
		"no change", and __Auth survives untouched.
		"""
		for field in PASSWORD_FIELDS:
			self._preserve_password_if_unchanged(field)

	def validate(self):
		"""Run invariants — but skip entirely when Frappe is the one driving the save."""
		if self._is_framework_driven_save():
			return
		self._validate_positive_int("price_scale_divisor")
		self._validate_positive_int("log_retention_days")
		self._validate_enabled_requires_keys()
		self._validate_inbound_api_key_format()

	def on_update(self):
		"""Append a Wave Sync Log audit row counting child-table rows after every save.

		Single DocTypes' "delete-children-then-reinsert" save flow is a known
		Frappe footgun: a throw in the middle of save can leave the form
		appearing empty even after a transaction rollback. This audit row is
		the canonical record of what was on file the moment the save committed,
		so any future "my rules disappeared" claim has an unambiguous answer.
		"""
		try:
			self._log_post_save_snapshot()
		except Exception:
			# Audit is belt-and-braces; never let a logging hiccup fail the save.
			frappe.log_error(
				title="wave_sync_hypa: settings post-save audit failed",
				message=frappe.get_traceback(),
			)

	def _preserve_password_if_unchanged(self, fieldname: str) -> None:
		"""Copy the encrypted-store value back into the in-memory field when the operator did not change it."""
		current = self.get(fieldname) or ""
		if current and not all(c == "*" for c in str(current)):
			# Operator typed a real new value (not empty, not the mask). Persist it as-is.
			return
		stored = self.get_password(fieldname, raise_exception=False)
		if stored:
			# Restore the stored cleartext so Frappe's save pipeline sees no change to __Auth.
			self.set(fieldname, stored)

	def _is_framework_driven_save(self) -> bool:
		"""Return True when the save originates from install / migrate / patch hooks."""
		flags = frappe.flags
		return bool(
			getattr(flags, "in_install", False)
			or getattr(flags, "in_migrate", False)
			or getattr(flags, "in_patch", False)
			or getattr(flags, "in_fixtures", False)
		)

	def _validate_positive_int(self, fieldname: str) -> None:
		"""Refuse zero or negative values for numeric fields that drive math or retention."""
		value = self.get(fieldname) or 0
		if value <= 0:
			frappe.throw(_("{0} must be greater than zero.").format(_(self.meta.get_label(fieldname))))

	def _validate_enabled_requires_keys(self) -> None:
		"""When the integration is enabled, an inbound API key must be persisted somewhere.

		Source of truth is the encrypted store (`get_password`), NOT the
		in-memory value. The form posts the field as the literal mask string,
		as None, or as an empty string depending on browser state, and on a
		child-table-only save the field is in any of those shapes without the
		operator having touched it. Throwing on those shapes was the root of
		the data-loss reports — the throw aborts the save mid-flight after
		Frappe has already deleted-and-not-yet-reinserted the child rows.

		So: if the encrypted store has anything, we accept the save.
		Otherwise we accept a fresh inline key (the format check runs next).
		Only throw when nothing is on file AND the operator did not type a
		new key on this save.
		"""
		if not self.enabled:
			return

		# Encrypted store is authoritative; if a key is on file, accept the save.
		stored = self.get_password("inbound_api_key", raise_exception=False)
		if stored:
			return

		# Nothing stored — accept only if the operator typed a real new value
		# (not the mask, not empty). Format is verified by the next validator.
		current = (self.inbound_api_key or "").strip()
		if current and not all(c == "*" for c in current):
			return

		frappe.throw(_("Inbound API Key is required when the integration is enabled."))

	def _validate_inbound_api_key_format(self) -> None:
		"""Require new inbound keys to be 32 URL-safe characters; skip masked / unchanged values."""
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
		if not INBOUND_API_KEY_PATTERN.match(current):
			frappe.throw(
				_(
					"Inbound API Key must contain only URL-safe characters: A-Z, a-z, 0-9, "
					"underscore, or hyphen. Generate a compliant key with: "
					"python -c 'import secrets; print(secrets.token_urlsafe(24))'"
				)
			)

	def _log_post_save_snapshot(self) -> None:
		"""Write one Wave Sync Log row recording who saved and how many rows each child table holds."""
		from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id
		from wave_sync_hypa.wave_sync_hypa.services.logger import log_step

		counts = {field: len(self.get(field) or []) for field in CHILD_TABLE_FIELDS}
		inbound_key_on_file = bool(self.get_password("inbound_api_key", raise_exception=False))
		log_step(
			correlation_id=new_correlation_id(),
			step="settings_post_save_snapshot",
			level="Info",
			doc_type="Wave Settings",
			linked_doctype="Wave Settings",
			linked_docname="Wave Settings",
			request_body={
				"saved_by": frappe.session.user,
				"enabled": bool(self.enabled),
				"inbound_api_key_on_file": inbound_key_on_file,
				"child_table_row_counts": counts,
			},
		)
