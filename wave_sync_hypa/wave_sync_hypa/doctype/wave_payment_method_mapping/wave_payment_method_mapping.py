"""Controller for Wave Payment Method Mapping (child of Wave Settings)."""

from frappe.model.document import Document


class WavePaymentMethodMapping(Document):
	"""Row-level controller. The intake handler + PE validator read these rows to map Wave's
	paymentType strings to a classification (prepaid|cod) and an ERP Mode of Payment."""

	pass
