"""Scheduled tasks for CODSync WooCommerce."""

import frappe

from codsync_woocommerce.codsync_woocommerce.engine import run_reconciliation
from codsync_woocommerce.codsync_woocommerce.matching import mark_missing


def run_hourly_failure_alert():
	"""Hourly scheduler job: email a digest of the last hour's Failed WC Sync Log rows."""
	try:
		from frappe.utils import add_to_date, now_datetime

		rows = frappe.get_all(
			"WC Sync Log",
			filters={"status": "Failed", "creation": [">", add_to_date(now_datetime(), hours=-1)]},
			fields=["creation", "platform", "operation", "error_message", "endpoint", "external_id"],
			order_by="creation desc",
			limit=50,
		)
		if not rows:
			return
		recipient = frappe.db.get_single_value("WC Store Connection", "alert_email") or frappe.db.get_value(
			"User", "Administrator", "email"
		)
		lines = []
		for r in rows:
			parts = [f"{r.creation}", f"{r.platform} {r.operation}"]
			if r.external_id:
				parts.append(str(r.external_id))
			if r.endpoint:
				parts.append(str(r.endpoint))
			head = " | ".join(parts)
			lines.append(head + (f"\n    {r.error_message}" if r.error_message else ""))
		frappe.sendmail(
			recipients=[recipient],
			subject=f"[CODSync WooCommerce] {len(rows)} sync failure(s) in the last hour",
			message="<pre style='font-size:12px'>" + frappe.utils.escape_html("\n".join(lines)) + "</pre>",
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "CODSync WooCommerce hourly failure alert")
		raise


def run_daily_order_sync():
	"""Daily scheduler job: re-pull orders from the connected store.

	Webhooks are the primary feed; this is the safety net so a missed delivery
	(WooCommerce gives up after retries) never leaves the ledger stale. Upserts by
	external_id, so re-running is idempotent.
	"""
	conn = frappe.get_doc("WC Store Connection")
	if not conn.enabled:
		return
	previous_user = frappe.session.user
	try:
		frappe.set_user("Administrator")  # nosemgrep: scheduled job, not a web request
		conn.fetch_orders()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "CODSync WooCommerce scheduled order sync")
	finally:
		# Frappe's worker commits the completed job, including WC Sync Log rows.
		frappe.set_user(previous_user)


def run_daily_mark_missing():
	"""Daily scheduler job: run mark_missing for every active WC Courier."""
	try:
		couriers = frappe.get_all(
			"WC Courier",
			filters={"is_active": 1},
			pluck="name",
		)
		for courier in couriers:
			try:
				mark_missing(courier)
			except Exception:
				frappe.log_error(
					frappe.get_traceback(),
					f"mark_missing failed for courier: {courier}",
				)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "CODSync WooCommerce daily mark missing")
		raise


def run_daily_reconciliation():
	"""Daily scheduler job: detectors (Missing settlement / Missing AWB / Tracking mismatch)."""
	try:
		run_reconciliation()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "CODSync WooCommerce run_reconciliation")
