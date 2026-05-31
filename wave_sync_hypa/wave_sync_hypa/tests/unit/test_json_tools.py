"""Unit tests for utils.json_tools.safe_dumps."""

import datetime
import decimal
import json

from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.utils.json_tools import safe_dumps


class TestSafeDumps(FrappeTestCase):
	"""safe_dumps must handle every value we could reasonably pass to log_step."""

	def test_none_returns_empty_string(self):
		"""None payloads are serialised as empty string so the DB column can be NOT NULL-safe."""
		self.assertEqual(safe_dumps(None), "")

	def test_plain_dict_round_trips(self):
		"""Serialising a plain dict produces valid JSON that can be loaded back."""
		payload = {"a": 1, "b": [2, 3], "c": {"d": "e"}}
		self.assertEqual(json.loads(safe_dumps(payload)), payload)

	def test_datetime_is_isoformatted(self):
		"""Datetimes are serialised using ISO 8601 so they remain human-readable."""
		when = datetime.datetime(2026, 4, 21, 12, 34, 56)
		self.assertIn("2026-04-21T12:34:56", safe_dumps({"when": when}))

	def test_decimal_becomes_string(self):
		"""Decimals are serialised as strings to preserve precision without type errors."""
		self.assertIn("\"1.50\"", safe_dumps({"amount": decimal.Decimal("1.50")}))

	def test_arbitrary_object_falls_back_to_str(self):
		"""Unknown object types don't crash the logger — they stringify."""

		class Custom:
			def __repr__(self):
				return "Custom()"

		self.assertIn("Custom()", safe_dumps({"obj": Custom()}))
