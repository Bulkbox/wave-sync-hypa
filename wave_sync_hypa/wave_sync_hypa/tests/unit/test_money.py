"""Unit tests for utils.money.cents_to_major."""

from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.utils.money import cents_to_major


class TestCentsToMajor(FrappeTestCase):
	"""Divide by the configured divisor; reject nonsensical inputs."""

	def test_typical_conversion(self):
		"""56010 cents at divisor=100 converts to 560.10 major units."""
		self.assertAlmostEqual(cents_to_major(56010, 100), 560.10, places=2)

	def test_zero_amount_returns_zero(self):
		"""Zero cents is a legitimate value (e.g. a fee line with no charge)."""
		self.assertEqual(cents_to_major(0, 100), 0.0)

	def test_none_amount_returns_zero(self):
		"""None (missing field) maps to 0.0 so the SO builder doesn't crash."""
		self.assertEqual(cents_to_major(None, 100), 0.0)

	def test_alternate_divisor(self):
		"""Three-decimal currencies use divisor=1000; 1234 minor units -> 1.234 major."""
		self.assertAlmostEqual(cents_to_major(1234, 1000), 1.234, places=3)

	def test_non_positive_divisor_raises(self):
		"""A zero or negative divisor is a configuration bug and must fail loudly."""
		with self.assertRaises(ValueError):
			cents_to_major(100, 0)
		with self.assertRaises(ValueError):
			cents_to_major(100, -100)
