"""WooCommerce REST v3 orders and signed webhooks. No dependency on Shopify CODSync."""

import base64
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import frappe
import requests
from frappe.utils import flt, get_datetime, now_datetime

from codsync_woocommerce.codsync_woocommerce.platforms.base import (
	BasePlatform,
	get_creds,
	normalize_store_url,
)
from codsync_woocommerce.codsync_woocommerce.synclog import log_sync


class WooCommerceError(RuntimeError):
	"""An API error safe to put in logs without credentials or response bodies."""


class WooCommercePlatform(BasePlatform):
	name = "WooCommerce"

	def _request(self, conn, resource, params=None):
		creds = get_creds(conn)
		if not creds["client_id"] or not creds["client_secret"]:
			raise WooCommerceError("Configure the WooCommerce consumer key and consumer secret first")
		if creds["api_version"] != "wc/v3":
			raise WooCommerceError("Only the WooCommerce wc/v3 API is supported")
		url = normalize_store_url(conn.store_url) + "/wp-json/wc/v3/" + resource
		try:
			response = requests.get(
				url,
				auth=(creds["client_id"], creds["client_secret"]),
				params=params,
				timeout=30,
				allow_redirects=False,
			)
		except requests.RequestException:
			raise WooCommerceError("WooCommerce request could not be completed") from None
		if not 200 <= response.status_code < 300:
			raise WooCommerceError(f"WooCommerce request failed (HTTP {response.status_code})")
		try:
			data = response.json()
		except ValueError:
			raise WooCommerceError("WooCommerce returned an invalid JSON response") from None
		if not isinstance(data, list):
			raise WooCommerceError("WooCommerce returned an unexpected orders response")
		return data, response.headers

	def test_connection(self, conn):
		try:
			self._request(conn, "orders", {"per_page": 1})
		except (WooCommerceError, ValueError) as error:
			ok, message = False, str(error)
		else:
			ok, message = True, "Connected to WooCommerce; order access verified"
		frappe.db.set_single_value(
			"WC Store Connection",
			{"last_test_status": "Success" if ok else "Failed", "last_tested_on": now_datetime()},
		)
		log_sync(
			self.name, "Test Connection", "Success" if ok else "Failed", error_message=None if ok else message
		)
		return ok, message

	def verify_webhook(self, headers, raw_body, conn):
		secret = get_creds(conn)["webhook_secret"]
		if not secret:
			return False
		try:
			source = normalize_store_url(headers.get("X-WC-Webhook-Source") or "")
			if source != normalize_store_url(conn.store_url):
				return False
		except ValueError:
			return False
		expected = base64.b64encode(hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()).decode()
		try:
			return hmac.compare_digest(expected, str(headers.get("X-WC-Webhook-Signature") or ""))
		except TypeError:
			return False

	def parse_order(self, payload, conn):
		if (
			not isinstance(payload, dict)
			or not str(payload.get("id") or "").isdigit()
			or int(payload["id"]) <= 0
		):
			raise ValueError("WooCommerce order must have a positive numeric id")
		meta = {
			item["key"]: item.get("value")
			for item in payload.get("meta_data") or []
			if isinstance(item, dict) and "key" in item
		}
		tracking = meta.get(conn.get("tracking_metadata_key") or "_wc_shipment_tracking_items") or []
		if isinstance(tracking, str):
			try:
				tracking = frappe.parse_json(tracking)
			except ValueError:
				tracking = []
		shipment = (
			next((item for item in tracking if isinstance(item, dict)), {})
			if isinstance(tracking, list)
			else {}
		)
		number = shipment.get("tracking_number") or meta.get(
			conn.get("tracking_number_key") or "_tracking_number"
		)
		provider = (
			shipment.get("custom_tracking_provider")
			or shipment.get("tracking_provider")
			or meta.get(conn.get("tracking_provider_key") or "_tracking_provider")
		)
		delivered = meta.get(conn.get("delivered_date_key") or "_delivered_date")
		billing, shipping = payload.get("billing") or {}, payload.get("shipping") or {}
		address = shipping if shipping.get("address_1") or shipping.get("first_name") else billing
		created = payload.get("date_created_gmt") or payload.get("date_created")
		original = flt(payload.get("total"))
		from codsync_woocommerce.codsync_woocommerce.currency import convert_to_base, get_base_currency

		base = get_base_currency(conn)
		currency = payload.get("currency") or base
		total, rate, rate_source = convert_to_base(
			original, currency, get_datetime(created).date() if created else None, base
		)
		cod_methods = {
			part.strip().lower()
			for part in (conn.get("cod_payment_methods") or "cod").split(",")
			if part.strip()
		}
		is_cod = str(payload.get("payment_method") or "").lower() in cod_methods and total > 0
		status = payload.get("status") or "pending"
		if status in ("cancelled", "failed", "trash"):
			order_status = "Cancelled"
		elif status == "refunded":
			order_status = "Returned"
		else:
			order_status = "Fulfilled" if status == "completed" or number else "Unfulfilled"
		values = {
			"order_id": str(payload["id"]),
			"platform": self.name,
			"external_id": str(payload["id"]),
			"order_status": order_status,
			"source_status": status,
			"customer_name": " ".join(
				str(address.get(k) or "").strip() for k in ("first_name", "last_name")
			).strip(),
			"phone": shipping.get("phone") or billing.get("phone") or "",
			"email": billing.get("email") or "",
			"address": ", ".join(str(address.get(k)) for k in ("address_1", "address_2") if address.get(k)),
			"city": address.get("city") or "",
			"pincode": address.get("postcode") or "",
			"payment_mode": "COD" if is_cod else "Prepaid",
			"cod_amount": total if is_cod else 0,
			"order_total": total,
			"order_currency": currency,
			"original_amount": original,
			"exchange_rate": rate,
			"rate_source": rate_source,
			"awb": "".join(str(number).split()).upper() if number else None,
			"tracking_company": str(provider).strip() if provider else None,
			"declared_weight": flt(meta.get(conn.get("declared_weight_key") or "_declared_weight_kg")),
		}
		if created:
			values["order_date"] = get_datetime(created).replace(tzinfo=None)
		if payload.get("date_modified_gmt"):
			values["source_modified_gmt"] = get_datetime(payload["date_modified_gmt"]).replace(tzinfo=None)
		if delivered:
			values.update(status="Delivered", delivered_date=get_datetime(delivered).date())
		return values

	def fetch_orders(self, conn):
		from codsync_woocommerce.codsync_woocommerce.api import _upsert_order

		if not conn.enabled:
			raise WooCommerceError("Enable the WooCommerce connection before fetching orders")
		end = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
		params = {
			"per_page": 100,
			"page": 1,
			"orderby": "id",
			"order": "asc",
			"dates_are_gmt": True,
			"modified_before": end.isoformat(),
		}
		if conn.get("last_order_sync_gmt"):
			params["modified_after"] = (
				get_datetime(conn.last_order_sync_gmt) - timedelta(seconds=1)
			).isoformat()
		counts = {"created": 0, "updated": 0, "unchanged": 0, "failed": 0}
		try:
			while True:
				orders, headers = self._request(conn, "orders", params)
				for payload in orders:
					frappe.db.savepoint("wc_order_sync")
					try:
						outcome = _upsert_order(self.parse_order(payload, conn), conn)
					except Exception:
						frappe.db.rollback(save_point="wc_order_sync")
						counts["failed"] += 1
						frappe.log_error(frappe.get_traceback(), "WooCommerce order sync row failed")
					else:
						counts[outcome] += 1
				pages = int(headers.get("X-WP-TotalPages") or 0)
				if not orders or (pages and params["page"] >= pages) or (not pages and len(orders) < 100):
					break
				params["page"] += 1
		except Exception:
			log_sync(
				self.name,
				"Order Sync",
				"Failed",
				error_message="Order sync interrupted; the sync cursor was not advanced",
			)
			raise
		if not counts["failed"]:
			frappe.db.set_single_value(
				"WC Store Connection", {"last_order_sync_gmt": end, "last_synced_on": now_datetime()}
			)
		log_sync(
			self.name, "Order Sync", "Failed" if counts["failed"] else "Success", response_payload=counts
		)
		return counts

	def order_url(self, external_id, conn):
		# WooCommerce redirects the legacy post editor link to HPOS when enabled.
		return (
			normalize_store_url(conn.store_url)
			+ "/wp-admin/post.php?post="
			+ quote(str(external_id), safe="")
			+ "&action=edit"
		)
