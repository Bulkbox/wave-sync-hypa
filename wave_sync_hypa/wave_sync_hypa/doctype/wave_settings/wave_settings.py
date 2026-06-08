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
		"""Restore Password fields and child-table rows that the in-memory doc has dropped.

		Two distinct preservation passes, both addressing the same Frappe
		footgun: the save pipeline treats whatever's in-memory as the new
		ground truth and wipes anything missing.

		1. Password fields. Browsers / partial form posts routinely send
		   inbound_api_key as None / "" even when the operator never touched
		   it. Without a guard, save would DELETE the row from __Auth.
		   Fix: when the in-memory value is empty / None / fully masked AND
		   the encrypted store still has a value, copy the cleartext back
		   in. Frappe re-encrypts the same value, sees no change, __Auth
		   survives.

		2. Child tables. The migrate / install / patch / fixtures pipelines
		   AND uncoordinated test runs against the same site sometimes
		   load Wave Settings with child-table attributes empty (operator
		   never opened the doc; framework just calls save to re-apply
		   schema defaults; or a test deliberately blanks the table for
		   its own assertion). Frappe then DELETEs all child rows for
		   those tables because the in-memory list is empty. We've now
		   lost route_rules at least three times this way.
		   Fix: protect on EVERY save, not just framework-driven. The
		   "operator legitimately wants to clear a table" UX is an edge
		   case (operators delete rows individually); the cost-benefit
		   strongly favours protection. To explicitly clear a table now,
		   use SQL or set self.flags.allow_child_table_clear = True
		   before save (used by the audit-test that pins this behaviour).
		"""
		for field in PASSWORD_FIELDS:
			self._preserve_password_if_unchanged(field)
		if not getattr(self.flags, "allow_child_table_clear", False):
			for field in CHILD_TABLE_FIELDS:
				self._preserve_child_table_if_unchanged(field)

	def validate(self):
		"""Run invariants — but skip entirely when Frappe is the one driving the save."""
		if self._is_framework_driven_save():
			return
		self._validate_positive_int("price_scale_divisor")
		self._validate_positive_int("log_retention_days")
		self._validate_enabled_requires_keys()
		self._validate_inbound_api_key_format()
		self._validate_intake_defaults()

	def _validate_intake_defaults(self) -> None:
		"""Block saves where a configured order-intake default points at an unusable record.

		These five drive every ORDER.CREATE; a typo'd / disabled / wrong-company
		value would otherwise save fine and then hard-fail every order at Sales
		Order insert. When the integration is enabled they must also be present
		(can't go live with a hole); while disabled, blanks are allowed so initial
		setup isn't blocked, but any value that IS set is still validated.
		"""
		required = ("default_company", "default_warehouse", "default_price_list", "default_currency", "walk_in_customer")
		problems: list[str] = []

		if self.get("enabled"):
			problems += [
				_("{0} is required to enable the integration.").format(_(self.meta.get_label(field)))
				for field in required
				if not self.get(field)
			]

		company = self.get("default_company")
		if company and not frappe.db.exists("Company", company):
			problems.append(_("Default Company {0} does not exist.").format(company))

		warehouse = self.get("default_warehouse")
		if warehouse:
			wh = frappe.db.get_value("Warehouse", warehouse, ["is_group", "disabled", "company"], as_dict=True)
			if not wh:
				problems.append(_("Default Warehouse {0} does not exist.").format(warehouse))
			elif wh.is_group:
				problems.append(_("Default Warehouse {0} is a group warehouse; choose a leaf warehouse.").format(warehouse))
			elif wh.disabled:
				problems.append(_("Default Warehouse {0} is disabled.").format(warehouse))
			elif company and wh.company != company:
				problems.append(
					_("Default Warehouse {0} belongs to {1}, not Default Company {2}.").format(warehouse, wh.company, company)
				)

		price_list = self.get("default_price_list")
		if price_list:
			pl = frappe.db.get_value("Price List", price_list, ["enabled", "selling"], as_dict=True)
			if not pl:
				problems.append(_("Default Price List {0} does not exist.").format(price_list))
			elif not pl.enabled:
				problems.append(_("Default Price List {0} is disabled.").format(price_list))
			elif not pl.selling:
				problems.append(_("Default Price List {0} is not a Selling price list.").format(price_list))

		currency = self.get("default_currency")
		if currency:
			cur = frappe.db.get_value("Currency", currency, ["enabled"], as_dict=True)
			if not cur:
				problems.append(_("Default Currency {0} does not exist.").format(currency))
			elif not cur.enabled:
				problems.append(_("Default Currency {0} is disabled.").format(currency))

		walk_in = self.get("walk_in_customer")
		if walk_in:
			wc = frappe.db.get_value("Customer", walk_in, ["disabled"], as_dict=True)
			if wc is None:
				problems.append(_("Walk-in Customer {0} does not exist.").format(walk_in))
			elif wc.disabled:
				problems.append(_("Walk-in Customer {0} is disabled.").format(walk_in))

		if problems:
			frappe.throw(
				_("Wave Settings cannot be saved — fix the order-intake defaults:")
				+ "<br>&bull; "
				+ "<br>&bull; ".join(problems),
				title=_("Invalid Wave intake configuration"),
			)

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

	def _preserve_child_table_if_unchanged(self, tablefield: str) -> None:
		"""Repopulate an in-memory child table from DB when it's empty but the DB has rows.

		Called on EVERY save unless the caller sets
		`self.flags.allow_child_table_clear = True`. Operators who want to
		clear a table do so by deleting rows in the form (each delete +
		save still leaves the other rows intact); they don't blank the
		whole table in one action. Framework-driven saves (migrate /
		install / patch / fixtures) and uncoordinated test runs both load
		the doc with empty in-memory child lists, and without this guard
		Frappe DELETEs every row from the corresponding child tables.
		"""
		current = self.get(tablefield) or []
		if current:
			return
		child_doctype = self.meta.get_field(tablefield).options
		stored_rows = frappe.get_all(
			child_doctype,
			filters={"parent": self.name, "parenttype": self.doctype, "parentfield": tablefield},
			fields=["name"],
			order_by="idx asc",
		)
		if not stored_rows:
			return
		for row in stored_rows:
			existing = frappe.get_doc(child_doctype, row.name)
			self.append(tablefield, existing.as_dict())

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
