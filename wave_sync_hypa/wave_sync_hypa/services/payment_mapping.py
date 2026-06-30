"""Single home for resolving a Wave paymentType to an ERPNext Mode of Payment.

The mapping lives in Wave Settings.payment_method_mappings (rows of
wave_payment_type -> mode_of_payment + classification). The prepaid PE creator
and the payment validator both resolve the Mode of Payment through here so the
rule has one source of truth.
"""

from __future__ import annotations


def mode_of_payment_for(settings, payment_type) -> str | None:
	"""Mode of Payment mapped to this Wave paymentType on the given settings, or None."""
	payment_type = (payment_type or "").strip()
	if not payment_type:
		return None
	for row in settings.get("payment_method_mappings") or []:
		if (row.get("wave_payment_type") or "").strip() == payment_type:
			return (row.get("mode_of_payment") or "").strip() or None
	return None
