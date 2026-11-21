"""Database-backed order ingestion and ledger preservation; no WooCommerce network calls."""

from datetime import datetime

import frappe

from codsync_woocommerce.codsync_woocommerce import api
from codsync_woocommerce.tests.doctype_case import DocTypeCase


class TestOrderIngestion(DocTypeCase):
	prefix = "WCING"

	def setUp(self):
		super().setUp()
		candidate = 999999999999
		while frappe.db.exists("WC COD Parcel", str(candidate)):
			candidate += 1
		self.order_id = str(candidate)

	def values(self, **overrides):
		return {
			"order_id": self.order_id,
			"external_id": self.order_id,
			"platform": "WooCommerce",
			"payment_mode": "COD",
			"cod_amount": 1000,
			"order_total": 1000,
			"awb": "WCINGAWB123",
			"tracking_company": self.fx.courier,
			"order_status": "Fulfilled",
			"source_status": "processing",
			"source_modified_gmt": datetime(2026, 11, 18, 10),
			**overrides,
		}

	def test_duplicate_delivery_updates_same_parcel_without_resetting_settlement(self):
		conn = frappe.get_doc("WC Store Connection")
		self.assertEqual(api._upsert_order(self.values(), conn), "created")
		frappe.db.set_value(
			"WC COD Parcel",
			"123",
			{
				"expected_settlement": 900,
				"actual_settlement": 850,
				"difference": -50,
				"payout_status": "Short-paid",
			},
		)
		self.assertEqual(api._upsert_order(self.values(), conn), "updated")
		self.assertEqual(frappe.db.count("WC COD Parcel", {"order_id": self.order_id}), 1)
		parcel = frappe.get_doc("WC COD Parcel", self.order_id)
		self.assertEqual(
			(parcel.expected_settlement, parcel.actual_settlement, parcel.difference, parcel.payout_status),
			(900, 850, -50, "Short-paid"),
		)

	def test_out_of_order_delivery_keeps_newer_order(self):
		conn = frappe.get_doc("WC Store Connection")
		api._upsert_order(self.values(), conn)
		result = api._upsert_order(
			self.values(cod_amount=500, source_modified_gmt=datetime(2026, 11, 17, 10)), conn
		)
		self.assertEqual(result, "unchanged")
		self.assertEqual(frappe.db.get_value("WC COD Parcel", self.order_id, "cod_amount"), 1000)

	def test_deleted_order_preserves_financial_record(self):
		conn = frappe.get_doc("WC Store Connection")
		api._upsert_order(self.values(), conn)
		frappe.db.set_value("WC COD Parcel", self.order_id, "actual_settlement", 900)
		self.fx.set_settings(enabled=1)
		api.process_order({"id": int(self.order_id)}, deleted=True)
		parcel = frappe.get_doc("WC COD Parcel", self.order_id)
		self.assertEqual((parcel.status, parcel.actual_settlement), ("Cancelled", 900))
