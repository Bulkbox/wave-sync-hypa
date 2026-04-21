"""JSON serialisation helpers tolerant of Frappe / Wave payload quirks."""

import datetime
import decimal
import json
from typing import Any


def safe_dumps(value: Any) -> str:
	"""Serialise any payload to a JSON string without raising on odd types."""
	if value is None:
		return ""
	return json.dumps(value, default=_fallback, ensure_ascii=False)


def _fallback(value: Any) -> str:
	"""Convert non-JSON-native values to strings (datetime, Decimal, and anything else)."""
	if isinstance(value, datetime.datetime | datetime.date):
		return value.isoformat()
	if isinstance(value, decimal.Decimal):
		return str(value)
	return str(value)
