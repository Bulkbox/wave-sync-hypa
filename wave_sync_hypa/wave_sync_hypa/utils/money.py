"""Money-unit conversion helpers."""


def cents_to_major(amount_cents: int | float | None, divisor: int) -> float:
	"""Convert a Wave amount in minor units (cents) to major currency units using the divisor."""
	if amount_cents is None:
		return 0.0
	if divisor <= 0:
		raise ValueError("divisor must be positive")
	return float(amount_cents) / float(divisor)
