"""Controller for Wave Status Rule Outbound (child of Wave Settings)."""

from frappe.model.document import Document


class WaveStatusRuleOutbound(Document):
	"""Row-level controller. The outbound service reads these rows to pick the Wave status to push."""

	pass
