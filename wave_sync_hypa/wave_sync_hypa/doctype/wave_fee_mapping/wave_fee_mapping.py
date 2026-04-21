"""Controller for Wave Fee Mapping (child of Wave Settings)."""

from frappe.model.document import Document


class WaveFeeMapping(Document):
	"""Row-level controller. The fee resolver reads these rows to pick an ERP Item for a Wave fee."""

	pass
