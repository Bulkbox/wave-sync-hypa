"""Controller for the Wave Delivery Status catalogue DocType.

Pure data carrier: no validation hooks beyond what the JSON enforces (unique
status_name). Existence-as-DocType lets admins extend the catalogue if Wave
adds a new deliveryStatus value, without a code release.
"""

from frappe.model.document import Document


class WaveDeliveryStatus(Document):
	"""Catalogue row representing one valid deliveryStatus string accepted by Wave."""

	pass
