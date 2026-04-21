"""Controller for the Wave Status catalogue DocType."""

from frappe.model.document import Document


class WaveStatus(Document):
	"""Row-level controller. No behaviour required; the rule tables read status_name via Link."""

	pass
