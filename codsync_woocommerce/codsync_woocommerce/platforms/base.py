"""WooCommerce connection settings; credentials stay in Frappe's encrypted password store."""

from urllib.parse import urlsplit, urlunsplit

import frappe


def normalize_store_url(value):
	value = (value or "").strip().rstrip("/")
	if "://" not in value:
		value = "https://" + value
	parts = urlsplit(value)
	if (
		parts.scheme != "https"
		or not parts.hostname
		or parts.username
		or parts.password
		or parts.query
		or parts.fragment
	):
		raise ValueError("Store URL must be an HTTPS URL without credentials, a query, or a fragment")
	return urlunsplit(("https", parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def get_creds(conn):
	def password(field):
		return conn.get_password(field, raise_exception=False) or None

	return {
		"client_id": password("woo_consumer_key"),
		"client_secret": password("woo_consumer_secret"),
		"api_version": conn.get("woo_api_version") or "wc/v3",
		"webhook_secret": password("webhook_secret"),
		"rate_api_key": password("rate_api_key"),
		"extra": {},
	}


class BasePlatform:
	name = None

	def verify_webhook(self, headers, raw_body, conn):
		raise NotImplementedError

	def parse_order(self, payload, conn):
		raise NotImplementedError

	def test_connection(self, conn):
		raise NotImplementedError

	def fetch_orders(self, conn):
		raise NotImplementedError

	def order_url(self, external_id, conn):
		return None
