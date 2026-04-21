"""Handler-level idempotency check for the Wave <> Hypa pipeline.

Queue-level dedup (via `frappe.enqueue(..., job_name=...)`) protects against
re-enqueuing the same payload. This module covers the complementary case: the
webhook is received and enqueued twice, but the first run already persisted a
Completed row for the same (wave_id, updated_at) pair.
"""

import frappe


def is_duplicate(wave_id: str | None, wave_updated_at: str | None) -> bool:
	"""Return True if Wave Sync Log already has a Completed row for (wave_id, updated_at)."""
	if not wave_id or not wave_updated_at:
		return False
	return bool(
		frappe.db.exists(
			"Wave Sync Log",
			{
				"wave_id": wave_id,
				"wave_updated_at": str(wave_updated_at),
				"step": "Completed",
			},
		)
	)
