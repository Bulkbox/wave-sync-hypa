"""Unit tests for services.correlation.new_correlation_id."""

from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id


class TestNewCorrelationId(FrappeTestCase):
	"""Every correlation id must be a 32-char hex string and unique per call."""

	def test_length_and_charset(self):
		"""A UUID4 hex is exactly 32 lowercase hex characters."""
		value = new_correlation_id()
		self.assertEqual(len(value), 32)
		self.assertTrue(all(c in "0123456789abcdef" for c in value))

	def test_values_are_unique(self):
		"""Two calls never collide within a single process run."""
		self.assertNotEqual(new_correlation_id(), new_correlation_id())
