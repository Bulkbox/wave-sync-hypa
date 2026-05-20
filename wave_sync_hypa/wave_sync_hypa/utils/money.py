"""Money-unit conversion helpers."""


def cents_to_major(amount_cents: int | float | None, divisor: int) -> float:
	"""Convert a Wave amount in minor units (cents) to major currency units using the divisor."""
	if amount_cents is None:
		return 0.0
	if divisor <= 0:
		raise ValueError("divisor must be positive")
	return float(amount_cents) / float(divisor)


def major_to_cents(amount_major: float | int | None, divisor: int) -> int:
	"""Convert an ERP amount in major currency units to Wave's minor units (cents), rounded.

	Inverse of cents_to_major. Used by outbound ERP -> Wave pushes that need
	to express line prices in cents (beginPrice / finalPrice / totalPrice on
	Wave's OrderV3). None is treated as 0; negative values are passed through
	(caller decides whether negative pricing is a domain error).
	"""
	if amount_major is None:
		return 0
	if divisor <= 0:
		raise ValueError("divisor must be positive")
	return int(round(float(amount_major) * float(divisor)))
