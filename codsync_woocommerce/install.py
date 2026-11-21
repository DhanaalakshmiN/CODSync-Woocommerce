"""Install only this app's roles; do not require or mutate Shopify CODSync."""

import frappe

ROLES = ("CODSync WooCommerce Manager", "CODSync WooCommerce Operator", "CODSync WooCommerce Viewer")


def before_install():
	for role in ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
				ignore_permissions=True
			)
