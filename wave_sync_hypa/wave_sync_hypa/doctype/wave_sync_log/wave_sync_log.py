"""Controller for Wave Sync Log.

The DocType is append-only from the app's point of view: rows are written by
`services.logger.log_step` and deleted only by the daily retention task. The
controller therefore holds no behaviour — leaving it empty is intentional.
"""

from frappe.model.document import Document


class WaveSyncLog(Document):
	"""Read-only audit row. All writes flow through services.logger."""

	pass
