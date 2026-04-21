"""Controller for Wave Route Rule (child of Wave Settings)."""

from frappe.model.document import Document


class WaveRouteRule(Document):
	"""Row-level controller. No behaviour required; routing happens in services.dispatcher."""

	pass
