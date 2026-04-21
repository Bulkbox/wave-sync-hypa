"""Daily retention task for Wave Sync Log."""

import frappe
from frappe.utils import add_days, now_datetime


def purge_old_logs() -> int:
	"""Delete Wave Sync Log rows older than `Wave Settings.log_retention_days`; return the count."""
	retention_days = _get_retention_days()
	cutoff = add_days(now_datetime(), -retention_days)
	return _delete_logs_older_than(cutoff)


def _get_retention_days() -> int:
	"""Read the configured retention window from Wave Settings, defaulting to 14 days."""
	configured = frappe.db.get_single_value("Wave Settings", "log_retention_days")
	return int(configured) if configured and int(configured) > 0 else 14


def _delete_logs_older_than(cutoff) -> int:
	"""Delete Wave Sync Log rows whose `creation` is strictly older than `cutoff`; return the count."""
	stale_names = frappe.get_all(
		"Wave Sync Log",
		filters={"creation": ["<", cutoff]},
		pluck="name",
		limit_page_length=0,
	)
	for name in stale_names:
		frappe.delete_doc("Wave Sync Log", name, ignore_permissions=True, delete_permanently=True)
	if stale_names:
		frappe.db.commit()
	return len(stale_names)
