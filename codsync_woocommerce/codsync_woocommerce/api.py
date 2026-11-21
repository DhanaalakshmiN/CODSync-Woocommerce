"""Signed WooCommerce webhooks and manager-only order sync for this app's own records."""

import json
import re
from datetime import UTC, datetime

import frappe
from frappe import _
from frappe.utils import flt, get_datetime

from codsync_woocommerce.codsync_woocommerce.platforms import get_platform
from codsync_woocommerce.codsync_woocommerce.synclog import log_sync


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook():
	"""The unsigned registration ping is read-only; order events must pass HMAC validation."""
	raw = frappe.request.get_data()
	if re.fullmatch(rb"webhook_id=[1-9][0-9]*", raw):
		return {"status": "ok"}
	conn = frappe.get_cached_doc("WC Store Connection")
	if not conn.enabled:
		frappe.response["http_status_code"] = 403
		return {"error": "Store connection is disabled"}
	adapter = get_platform(conn.platform)
	if not adapter.verify_webhook(frappe.request.headers, raw, conn):
		log_sync(
			"WooCommerce", "Webhook Order", "Failed", error_message="Invalid webhook signature or source"
		)
		frappe.response["http_status_code"] = 401
		return {"error": "Invalid webhook signature or source"}
	topic = frappe.request.headers.get("X-WC-Webhook-Topic")
	if topic not in ("order.created", "order.updated", "order.deleted"):
		frappe.response["http_status_code"] = 400
		return {"error": "Unsupported webhook topic"}
	try:
		payload = json.loads(raw)
		if (
			not isinstance(payload, dict)
			or not str(payload.get("id") or "").isdigit()
			or int(payload["id"]) <= 0
		):
			raise ValueError
	except ValueError, TypeError:
		frappe.response["http_status_code"] = 400
		return {"error": "Invalid order payload"}
	frappe.enqueue(
		"codsync_woocommerce.codsync_woocommerce.api.process_order",
		payload=payload,
		deleted=topic == "order.deleted",
		queue="short",
		enqueue_after_commit=True,
	)
	log_sync(
		"WooCommerce",
		"Webhook Order",
		"Success",
		external_id=str(payload["id"]),
		response_payload={"topic": topic},
	)
	return {"status": "queued"}


def process_order(payload, deleted=False):
	"""Worker input comes only from the signed webhook; records are isolated to WC DocTypes."""
	conn = frappe.get_cached_doc("WC Store Connection")
	if not conn.enabled:
		return
	try:
		if deleted:
			name = str(payload["id"])
			if frappe.db.exists("WC COD Parcel", name):
				frappe.db.set_value(
					"WC COD Parcel",
					name,
					{
						"order_status": "Cancelled",
						"status": "Cancelled",
						"source_status": "trash",
						"source_modified_gmt": datetime.now(UTC).replace(tzinfo=None),
					},
				)
			return
		_upsert_order(get_platform(conn.platform).parse_order(payload, conn), conn)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "WooCommerce webhook order processing failed")
		raise


def _upsert_order(values, conn):
	"""Idempotent upsert by stable WooCommerce numeric ID; ignore older webhook deliveries."""
	values = dict(values)
	name = values.pop("order_id")
	awb = values.pop("awb", None)
	company = values.pop("tracking_company", None)
	weight = flt(values.pop("declared_weight", None))
	exists = frappe.db.exists("WC COD Parcel", name)
	if exists:
		parcel = frappe.get_doc("WC COD Parcel", name)
		incoming, stored = values.get("source_modified_gmt"), parcel.get("source_modified_gmt")
		if incoming and stored and get_datetime(incoming) < get_datetime(stored):
			return "unchanged"
		parcel.update(values)
	else:
		parcel = frappe.get_doc({"doctype": "WC COD Parcel", "order_id": name, "status": "Booked", **values})
	if values.get("order_status") == "Cancelled":
		parcel.status = "Cancelled"
	if awb:
		owner = frappe.db.get_value("WC COD Parcel", {"awb": awb}, "name")
		if owner and owner != parcel.name and owner != name:
			frappe.throw(_("The AWB is already assigned to another WooCommerce order"))
		parcel.awb = awb
		parcel.courier = _resolve_courier(company, conn) or parcel.courier
	if weight > 0:
		parcel.declared_weight = weight
	# Verified webhook workers can run as Guest; the write target is fixed to this app's DocType.
	if exists:
		parcel.save(ignore_permissions=True)  # nosemgrep: verified WooCommerce webhook or manager-only sync
	else:
		parcel.insert(ignore_permissions=True)  # nosemgrep: verified WooCommerce webhook or manager-only sync
	return "updated" if exists else "created"


def _resolve_courier(source_name, conn):
	for row in conn.courier_mapping or []:
		if source_name and (row.source_name or "").strip().lower() == source_name.strip().lower():
			return row.courier
	if conn.default_courier:
		return conn.default_courier
	if source_name:
		if not frappe.db.exists("WC Courier", source_name):
			frappe.get_doc({"doctype": "WC Courier", "courier_name": source_name}).insert(
				ignore_permissions=True
			)  # nosemgrep: internal carrier creation for verified order ingestion
		return source_name
	return None


@frappe.whitelist(methods=["POST"])
def sync_orders():
	frappe.only_for(("System Manager", "CODSync WooCommerce Manager"))
	conn = frappe.get_doc("WC Store Connection")
	return get_platform(conn.platform).fetch_orders(conn)
