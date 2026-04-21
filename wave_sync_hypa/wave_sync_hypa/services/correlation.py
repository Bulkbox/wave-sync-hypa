"""Correlation-ID generation for the Wave <> Hypa pipeline."""

import uuid


def new_correlation_id() -> str:
	"""Return a fresh hex UUID4 used to link every log row from one webhook."""
	return uuid.uuid4().hex
