"""Unit tests for handlers.sales_order_ipay.fetch_ipay_on_prepaid_insert (issue #129).

The Sales Order.after_insert guard: enqueue an iPay fetch only for prepaid Wave
orders with a friendly id, the iPay flag on, and the master switch on.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa.wave_sync_hypa.handlers import sales_order_ipay

SO = "SAL-ORD-2026-00105"


def _doc(classification="prepaid", friendly="10000105"):
	values = {
		"wave_payment_classification": classification,
		"wave_friendly_id": friendly,
		"wave_correlation_id": "corr-intake",
	}
	doc = SimpleNamespace(doctype="Sales Order", name=SO)
	doc.get = lambda key, default=None: values.get(key, default)
	return doc


def _settings(ipay_enabled=1):
	s = MagicMock(name="WaveSettings")
	s.get.side_effect = lambda key, default=None: {"ipay_verification_enabled": ipay_enabled}.get(key, default)
	return s


class TestFetchIpayOnPrepaidInsert(FrappeTestCase):
	def test_enqueues_for_prepaid_wave_order(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(sales_order_ipay, "skip_if_disabled", return_value=False),
			patch.object(sales_order_ipay.ipay_payment_sync, "enqueue_fetch") as mock_enqueue,
		):
			sales_order_ipay.fetch_ipay_on_prepaid_insert(_doc())
		mock_enqueue.assert_called_once_with(SO, "corr-intake")

	def test_skips_non_prepaid(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(sales_order_ipay.ipay_payment_sync, "enqueue_fetch") as mock_enqueue,
		):
			sales_order_ipay.fetch_ipay_on_prepaid_insert(_doc(classification="cod"))
		mock_enqueue.assert_not_called()

	def test_skips_when_no_friendly_id(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(sales_order_ipay.ipay_payment_sync, "enqueue_fetch") as mock_enqueue,
		):
			sales_order_ipay.fetch_ipay_on_prepaid_insert(_doc(friendly=""))
		mock_enqueue.assert_not_called()

	def test_skips_when_ipay_flag_off(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings(ipay_enabled=0)),
			patch.object(sales_order_ipay.ipay_payment_sync, "enqueue_fetch") as mock_enqueue,
		):
			sales_order_ipay.fetch_ipay_on_prepaid_insert(_doc())
		mock_enqueue.assert_not_called()

	def test_skips_when_master_switch_off(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_settings()),
			patch.object(sales_order_ipay, "skip_if_disabled", return_value=True),
			patch.object(sales_order_ipay.ipay_payment_sync, "enqueue_fetch") as mock_enqueue,
		):
			sales_order_ipay.fetch_ipay_on_prepaid_insert(_doc())
		mock_enqueue.assert_not_called()
