"""WooCommerce adapter and webhook contracts. All network, database and queue calls are mocked."""

import base64
import hashlib
import hmac
import inspect
import json
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import frappe
import requests

from codsync_woocommerce.codsync_woocommerce import api
from codsync_woocommerce.codsync_woocommerce.platforms import get_platform
from codsync_woocommerce.codsync_woocommerce.platforms.base import normalize_store_url
from codsync_woocommerce.codsync_woocommerce.platforms.woocommerce import (
	WooCommerceError,
	WooCommercePlatform,
)

ADAPTER = "codsync_woocommerce.codsync_woocommerce.platforms.woocommerce"


class Connection(frappe._dict):
	def get_password(self, field, **kwargs):
		return {
			"woo_consumer_key": "test-key",
			"woo_consumer_secret": "test-secret",
			"webhook_secret": "test-webhook-secret",
		}.get(field)


def connection(**kwargs):
	return Connection(
		{
			"platform": "WooCommerce",
			"enabled": 1,
			"store_url": "https://store.example/shop",
			"woo_api_version": "wc/v3",
			"cod_payment_methods": "cod",
			"courier_mapping": [],
			**kwargs,
		}
	)


def order(**kwargs):
	return {
		"id": 123,
		"number": "custom-number",
		"total": "1000.00",
		"currency": "INR",
		"payment_method": "cod",
		"status": "processing",
		"billing": {
			"first_name": "Test",
			"last_name": "Customer",
			"email": "test@example.invalid",
			"address_1": "Billing address",
		},
		"shipping": {
			"first_name": "Test",
			"last_name": "Customer",
			"address_1": "Shipping address",
			"city": "Chennai",
			"postcode": "600001",
		},
		"meta_data": [],
		**kwargs,
	}


class TestWooCommerceAdapter(unittest.TestCase):
	def setUp(self):
		self.adapter = WooCommercePlatform()
		self.addCleanup(patch.stopall)
		patch(
			"codsync_woocommerce.codsync_woocommerce.currency.get_base_currency", return_value="INR"
		).start()
		patch(
			"codsync_woocommerce.codsync_woocommerce.currency.convert_to_base",
			side_effect=lambda amount, *args: (amount, 1, ""),
		).start()
		patch(f"{ADAPTER}.now_datetime", return_value=datetime(2026, 11, 19, 12)).start()

	def test_registry_is_independent(self):
		self.assertIsInstance(get_platform("WooCommerce"), WooCommercePlatform)
		with (
			patch("codsync_woocommerce.codsync_woocommerce.platforms._", side_effect=lambda text: text),
			patch("frappe.throw", side_effect=frappe.ValidationError),
		):
			with self.assertRaises(frappe.ValidationError):
				get_platform("Shopify")

	def test_url_preserves_wordpress_subdirectory(self):
		self.assertEqual(normalize_store_url("Store.Example/shop/"), "https://store.example/shop")
		for value in (
			"",
			"http://store.example",
			"https://user:password@store.example",
			"https://store.example?key=value",
			"https://store.example/#fragment",
		):
			with self.subTest(value=value), self.assertRaises(ValueError):
				normalize_store_url(value)

	def test_api_uses_https_basic_auth_and_no_credential_query(self):
		with patch(f"{ADAPTER}.requests.get") as get:
			get.return_value.status_code = 200
			get.return_value.json.return_value = []
			self.adapter._request(connection(), "orders", {"page": 1})
			self.assertEqual(get.call_args.args, ("https://store.example/shop/wp-json/wc/v3/orders",))
			self.assertEqual(get.call_args.kwargs["auth"], ("test-key", "test-secret"))
			self.assertFalse(get.call_args.kwargs["allow_redirects"])
			self.assertEqual(get.call_args.kwargs["params"], {"page": 1})

	def test_api_errors_do_not_expose_sensitive_response_or_exception(self):
		with patch(f"{ADAPTER}.requests.get") as get:
			get.return_value.status_code = 401
			get.return_value.text = "test-secret"
			with self.assertRaisesRegex(WooCommerceError, r"HTTP 401") as raised:
				self.adapter._request(connection(), "orders")
			self.assertNotIn("test-secret", str(raised.exception))
			get.side_effect = requests.RequestException("test-secret")
			with self.assertRaises(WooCommerceError) as raised:
				self.adapter._request(connection(), "orders")
			self.assertNotIn("test-secret", str(raised.exception))

	def test_completed_order_does_not_imply_courier_delivery(self):
		values = self.adapter.parse_order(order(status="completed"), connection())
		self.assertEqual((values["order_id"], values["external_id"]), ("123", "123"))
		self.assertEqual((values["payment_mode"], values["cod_amount"]), ("COD", 1000))
		self.assertEqual(values["order_status"], "Fulfilled")
		self.assertNotIn("delivered_date", values)
		self.assertNotIn("status", values)

	def test_prepaid_and_configurable_cod_gateways(self):
		self.assertEqual(
			self.adapter.parse_order(order(payment_method="stripe"), connection())["cod_amount"], 0
		)
		self.assertEqual(
			self.adapter.parse_order(
				order(payment_method="custom_cod"), connection(cod_payment_methods="cod, custom_cod")
			)["payment_mode"],
			"COD",
		)
		self.assertEqual(self.adapter.parse_order(order(total="0"), connection())["payment_mode"], "Prepaid")

	def test_tracking_extension_and_explicit_delivery_metadata(self):
		payload = order(
			meta_data=[
				{
					"key": "_wc_shipment_tracking_items",
					"value": [{"tracking_number": " ab 123 ", "tracking_provider": "Carrier"}],
				},
				{"key": "_delivered_date", "value": "2026-11-18"},
				{"key": "_declared_weight_kg", "value": "1.5"},
			]
		)
		values = self.adapter.parse_order(payload, connection())
		self.assertEqual(
			(values["awb"], values["tracking_company"], values["declared_weight"]), ("AB123", "Carrier", 1.5)
		)
		self.assertEqual(values["status"], "Delivered")

	def test_custom_tracking_fields_and_billing_fallback(self):
		values = self.adapter.parse_order(
			order(shipping={}, meta_data=[{"key": "awb_custom", "value": "xy 1"}]),
			connection(tracking_number_key="awb_custom"),
		)
		self.assertEqual(values["awb"], "XY1")
		self.assertEqual(values["address"], "Billing address")

	def test_cancelled_refunded_and_invalid_payloads(self):
		for status, expected in (
			("cancelled", "Cancelled"),
			("failed", "Cancelled"),
			("refunded", "Returned"),
		):
			self.assertEqual(
				self.adapter.parse_order(order(status=status), connection())["order_status"], expected
			)
		for payload in ({}, [], order(id=0), order(id="invalid")):
			with self.assertRaises(ValueError):
				self.adapter.parse_order(payload, connection())

	def test_signature_checks_body_and_source(self):
		body = b'{"id": 123}'
		sig = base64.b64encode(hmac.new(b"test-webhook-secret", body, hashlib.sha256).digest()).decode()
		headers = {"X-WC-Webhook-Source": "https://store.example/shop/", "X-WC-Webhook-Signature": sig}
		self.assertTrue(self.adapter.verify_webhook(headers, body, connection()))
		self.assertFalse(self.adapter.verify_webhook(headers, body + b" ", connection()))
		self.assertFalse(
			self.adapter.verify_webhook(
				{**headers, "X-WC-Webhook-Source": "https://other.example"}, body, connection()
			)
		)
		self.assertFalse(self.adapter.verify_webhook({}, body, connection()))

	def test_paginated_sync_advances_cursor_after_all_rows_succeed(self):
		with (
			patch.object(
				self.adapter,
				"_request",
				side_effect=[
					([order()], {"X-WP-TotalPages": "2"}),
					([order(id=124)], {"X-WP-TotalPages": "2"}),
				],
			) as request,
			patch(
				"codsync_woocommerce.codsync_woocommerce.api._upsert_order",
				side_effect=["created", "updated"],
			),
			patch(f"{ADAPTER}.frappe", MagicMock()) as mocked,
			patch(f"{ADAPTER}.log_sync"),
		):
			result = self.adapter.fetch_orders(connection(last_order_sync_gmt=datetime(2026, 11, 18)))
			self.assertEqual(result, {"created": 1, "updated": 1, "unchanged": 0, "failed": 0})
			self.assertEqual(request.call_count, 2)
			mocked.db.set_single_value.assert_called_once()
			self.assertIn("modified_after", request.call_args.args[2])

	def test_partial_sync_failure_preserves_cursor_and_rolls_back_row(self):
		with (
			patch.object(self.adapter, "_request", return_value=([order()], {"X-WP-TotalPages": "1"})),
			patch(
				"codsync_woocommerce.codsync_woocommerce.api._upsert_order",
				side_effect=ValueError("invalid row"),
			),
			patch(f"{ADAPTER}.frappe", MagicMock()) as mocked,
			patch(f"{ADAPTER}.log_sync"),
		):
			self.assertEqual(self.adapter.fetch_orders(connection())["failed"], 1)
			mocked.db.rollback.assert_called_once_with(save_point="wc_order_sync")
			mocked.db.set_single_value.assert_not_called()


class TestWooCommerceWebhooks(unittest.TestCase):
	def test_registration_ping_never_accesses_records_or_queue(self):
		with patch.object(api, "frappe", MagicMock()) as mocked:
			mocked.request.get_data.return_value = b"webhook_id=123"
			self.assertEqual(inspect.unwrap(api.webhook)(), {"status": "ok"})
			mocked.get_cached_doc.assert_not_called()
			mocked.enqueue.assert_not_called()

	def test_unverified_webhook_is_rejected_without_queueing(self):
		with (
			patch.object(api, "frappe", MagicMock()) as mocked,
			patch.object(api, "get_platform") as get_platform,
			patch.object(api, "log_sync"),
		):
			mocked.request.get_data.return_value = b'{"id":123}'
			mocked.get_cached_doc.return_value.enabled = True
			get_platform.return_value.verify_webhook.return_value = False
			self.assertIn("error", inspect.unwrap(api.webhook)())
			mocked.enqueue.assert_not_called()

	def test_verified_event_queues_only_after_commit(self):
		with (
			patch.object(api, "frappe", MagicMock()) as mocked,
			patch.object(api, "get_platform") as get_platform,
			patch.object(api, "log_sync"),
		):
			mocked.request.get_data.return_value = json.dumps(order()).encode()
			mocked.request.headers = {"X-WC-Webhook-Topic": "order.updated"}
			get_platform.return_value.verify_webhook.return_value = True
			self.assertEqual(inspect.unwrap(api.webhook)(), {"status": "queued"})
			self.assertTrue(mocked.enqueue.call_args.kwargs["enqueue_after_commit"])
			self.assertEqual(mocked.enqueue.call_args.kwargs["payload"]["id"], 123)

	def test_upsert_does_not_mutate_input_and_updates_existing_record(self):
		values = {
			"order_id": "123",
			"platform": "WooCommerce",
			"awb": None,
			"tracking_company": None,
			"cod_amount": 1000,
		}
		with patch.object(api, "frappe", MagicMock()) as mocked:
			mocked.db.exists.return_value = True
			mocked.get_doc.return_value.get.return_value = None
			self.assertEqual(api._upsert_order(values, connection()), "updated")
			self.assertEqual(values["order_id"], "123")
			mocked.get_doc.return_value.save.assert_called_once_with(ignore_permissions=True)

	def test_older_event_cannot_overwrite_newer_order(self):
		with patch.object(api, "frappe", MagicMock()) as mocked:
			mocked.db.exists.return_value = True
			mocked.get_doc.return_value.get.return_value = datetime(2026, 11, 19)
			result = api._upsert_order(
				{"order_id": "123", "source_modified_gmt": datetime(2026, 11, 18)}, connection()
			)
			self.assertEqual(result, "unchanged")
			mocked.get_doc.return_value.save.assert_not_called()

	def test_disabled_connection_cannot_queue_orders(self):
		with patch.object(api, "frappe", MagicMock()) as mocked:
			mocked.request.get_data.return_value = b'{"id":123}'
			mocked.get_cached_doc.return_value.enabled = False
			self.assertIn("error", inspect.unwrap(api.webhook)())
			mocked.enqueue.assert_not_called()

	def test_invalid_signed_payload_or_topic_is_not_queued(self):
		for body, topic in (
			(b"[]", "order.updated"),
			(b"not-json", "order.updated"),
			(b'{"id":123}', "customer.updated"),
		):
			with (
				self.subTest(body=body, topic=topic),
				patch.object(api, "frappe", MagicMock()) as mocked,
				patch.object(api, "get_platform") as adapter,
			):
				mocked.request.get_data.return_value = body
				mocked.request.headers = {"X-WC-Webhook-Topic": topic}
				adapter.return_value.verify_webhook.return_value = True
				self.assertIn("error", inspect.unwrap(api.webhook)())
				mocked.enqueue.assert_not_called()

	def test_deletion_keeps_ledger_record_and_settlement_amounts(self):
		with patch.object(api, "frappe", MagicMock()) as mocked:
			mocked.db.exists.return_value = True
			api.process_order({"id": 123}, deleted=True)
			args = mocked.db.set_value.call_args.args
			self.assertEqual(args[:2], ("WC COD Parcel", "123"))
			self.assertEqual(args[2]["status"], "Cancelled")
			self.assertNotIn("actual_settlement", args[2])
			mocked.delete_doc.assert_not_called()
