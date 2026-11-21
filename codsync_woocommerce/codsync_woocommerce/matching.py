import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, nowdate


@frappe.whitelist(methods=["POST"])
def run_matching(remittance: str):
	"""Match a WC Courier Remittance's lines to WC COD Parcels by AWB (idempotent).

	Returns {"matched", "short-paid", "unknown", "exceptions"} and writes one
	WC Sync Log row (operation Reconciliation).
	"""
	from codsync_woocommerce.codsync_woocommerce.engine import reconcile_remittance
	from codsync_woocommerce.codsync_woocommerce.synclog import log_sync

	frappe.only_for(("System Manager", "CODSync WooCommerce Manager", "CODSync WooCommerce Operator"))
	if not frappe.db.exists("WC Courier Remittance", remittance):
		frappe.throw(_("Remittance {0} not found").format(remittance))

	try:
		result = reconcile_remittance(remittance)
	except Exception as e:
		log_sync(
			"Courier",
			"Reconciliation",
			"Failed",
			reference_doctype="WC Courier Remittance",
			reference_name=remittance,
			error_message=str(e),
		)
		raise
	counts = result["counts"]
	log_sync(
		"Courier",
		"Reconciliation",
		"Success",
		reference_doctype="WC Courier Remittance",
		reference_name=remittance,
		error_message=f"{len(result['skipped'])} row(s) skipped" if result["skipped"] else None,
		response_payload={"batch_status": result["batch_status"], **counts, "skipped": result["skipped"]},
	)
	return counts


def reconcile_remittance(remittance_name):
	"""Match each remittance line to a WC COD Parcel by AWB (see engine.reconcile_remittance)."""
	from codsync_woocommerce.codsync_woocommerce.engine import reconcile_remittance as _reconcile

	return _reconcile(remittance_name)


def mark_missing(courier):
	"""Set payout_status for parcels of a courier that have no remittance line yet."""
	paid_awbs = set(
		frappe.db.sql_list(
			"""
			select rl.awb from `tabWC Remittance Line` rl
			join `tabWC Courier Remittance` cr on cr.name = rl.parent
			where cr.docstatus = 1 and cr.courier = %s
			""",
			courier,
		)
	)
	cycle_days = frappe.db.get_value("WC Courier", courier, "payout_cycle_days") or 0
	today = getdate(nowdate())

	parcels = frappe.get_all(
		"WC COD Parcel",
		filters={
			"courier": courier,
			"status": ["in", ["Delivered", "In Transit"]],
			"payment_mode": ["!=", "Prepaid"],
		},
		fields=["name", "awb", "status", "delivered_date"],
	)
	for p in parcels:
		if p.awb in paid_awbs:
			continue
		if p.status == "In Transit":
			status = "Not due"
		elif p.delivered_date and getdate(add_days(p.delivered_date, cycle_days)) < today:
			status = "Missing"
		else:
			status = "Awaiting payout"
		frappe.db.set_value("WC COD Parcel", p.name, "payout_status", status)
