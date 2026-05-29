"""Unit tests for services.dispatcher.resolve_handler."""

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.services import dispatcher


class TestResolveHandler(FrappeTestCase):
	"""Match rules against the Wave Settings table, honour enabled/disabled, fall back to None."""

	def setUp(self):
		"""Snapshot baseline settings, clear rules, and stub one handler we can assert on."""
		# Force handler registration up-front so snapshot captures the real callable,
		# not the None placeholder that exists before any resolve_handler call.
		dispatcher._ensure_handlers_loaded()
		self._original_rules = self._snapshot_rules()
		self._clear_rules()
		self._original_handler = dispatcher.HANDLER_REGISTRY.get("customer_upsert")
		dispatcher.HANDLER_REGISTRY["customer_upsert"] = self._stub_handler

	def tearDown(self):
		"""Restore the route_rules and the registry so later tests see no side effects."""
		self._restore_rules(self._original_rules)
		dispatcher.HANDLER_REGISTRY["customer_upsert"] = self._original_handler
		frappe.clear_document_cache("Wave Settings", "Wave Settings")

	def _snapshot_rules(self) -> list[dict]:
		"""Capture the current route_rules as plain dicts so they can be rewritten verbatim."""
		settings = frappe.get_single("Wave Settings")
		return [
			{
				"doc_type": row.doc_type,
				"action": row.action,
				"handler_key": row.handler_key,
				"enabled": row.enabled,
			}
			for row in (settings.route_rules or [])
		]

	def _clear_rules(self) -> None:
		"""Drop every route_rules row via direct DB writes so validate() is not invoked."""
		frappe.db.delete("Wave Route Rule", {"parent": "Wave Settings"})
		frappe.db.commit()
		frappe.clear_document_cache("Wave Settings", "Wave Settings")

	def _restore_rules(self, rows: list[dict]) -> None:
		"""Replace route_rules with the provided rows via direct child-table SQL.

		Direct SQL + explicit commit is required so the restoration survives
		FrappeTestCase's class-level `_rollback_db` cleanup. A controller-
		level `settings.save(...)` writes within the test's connection
		transaction; without an explicit commit, that work is rolled back at
		test teardown and every subsequent test in this run sees an empty
		route_rules table — leaking the test wipe into the live site.
		"""
		frappe.db.delete("Wave Route Rule", {"parent": "Wave Settings"})
		for idx, row in enumerate(rows):
			child = frappe.get_doc(
				{
					"doctype": "Wave Route Rule",
					"parent": "Wave Settings",
					"parenttype": "Wave Settings",
					"parentfield": "route_rules",
					"idx": idx + 1,
					**row,
				}
			)
			child.flags.ignore_links = True
			child.insert(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_document_cache("Wave Settings", "Wave Settings")

	def _add_rule(self, doc_type: str, action: str, handler_key: str, enabled: int = 1) -> None:
		"""Append one Wave Route Rule row through the ORM and save without validation."""
		settings = frappe.get_single("Wave Settings")
		settings.append(
			"route_rules",
			{"doc_type": doc_type, "action": action, "handler_key": handler_key, "enabled": enabled},
		)
		settings.flags.ignore_validate = True
		settings.save(ignore_permissions=True)
		frappe.clear_document_cache("Wave Settings", "Wave Settings")

	@staticmethod
	def _stub_handler(payload, correlation_id):
		"""No-op handler used to verify registry wiring; returns a tag we can assert on."""
		return "stub_called"

	def test_returns_none_when_no_rule_matches(self):
		"""Unrouted events dispatch to nothing, by design, so the processor logs Skipped."""
		self.assertIsNone(dispatcher.resolve_handler("CUSTOMER", "UPDATE"))

	def test_returns_handler_when_enabled_rule_matches(self):
		"""An enabled rule with a registered handler returns that callable."""
		self._add_rule("CUSTOMER", "UPDATE", "customer_upsert", enabled=1)
		resolved = dispatcher.resolve_handler("CUSTOMER", "UPDATE")
		self.assertIs(resolved, self._stub_handler)

	def test_disabled_rule_is_ignored(self):
		"""A matching row with enabled=0 is as good as absent."""
		self._add_rule("CUSTOMER", "UPDATE", "customer_upsert", enabled=0)
		self.assertIsNone(dispatcher.resolve_handler("CUSTOMER", "UPDATE"))

	def test_matches_are_scoped_to_doc_and_action(self):
		"""A rule for CUSTOMER/UPDATE does not respond to CUSTOMER/DELETE or ORDER/UPDATE."""
		self._add_rule("CUSTOMER", "UPDATE", "customer_upsert", enabled=1)
		self.assertIsNone(dispatcher.resolve_handler("CUSTOMER", "DELETE"))
		self.assertIsNone(dispatcher.resolve_handler("ORDER", "UPDATE"))

	def test_returns_none_when_key_not_registered(self):
		"""A rule pointing at a key whose callable is still None returns None (phase not landed yet)."""
		# payment_apply stays unregistered until Phase 8; simulate a rule pointing at it.
		# The action must exist in the Wave Action catalogue because the field is now a Link.
		self._add_rule("ORDER", "DELETE", "payment_apply", enabled=1)
		self.assertIsNone(dispatcher.resolve_handler("ORDER", "DELETE"))
