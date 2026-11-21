"""DocType rules: validation, uniqueness, computed fields, hooks and role permissions."""

import frappe
from frappe.tests import IntegrationTestCase

from codsync_woocommerce.tests.doctype_case import DocTypeCase


class TestWCCourier(DocTypeCase):
	prefix = "DTS"

	def test_courier_name_is_unique(self):
		with self.assertRaises(frappe.DuplicateEntryError):
			frappe.get_doc({"doctype": "WC Courier", "courier_name": self.fx.courier}).insert(
				ignore_permissions=True
			)


class TestPermissions(IntegrationTestCase):
	"""Role rules as configured on each DocType (read from metadata, so no user session or Redis needed)."""

	def can(self, doctype, role, ptype):
		return any(
			p.role == role and p.permlevel == 0 and p.get(ptype) for p in frappe.get_meta(doctype).permissions
		)

	def roles_with(self, doctype, ptype):
		return {p.role for p in frappe.get_meta(doctype).permissions if p.permlevel == 0 and p.get(ptype)}

	def test_viewer_reads_but_cannot_write(self):
		for dt in (
			"WC COD Parcel",
			"WC COD Exception",
			"WC Courier",
			"WC Courier Remittance",
			"WC Currency Rate",
		):
			self.assertTrue(self.can(dt, "CODSync WooCommerce Viewer", "read"), dt)
			for ptype in ("write", "create", "delete"):
				self.assertFalse(self.can(dt, "CODSync WooCommerce Viewer", ptype), f"{dt} {ptype}")

	def test_operator_can_work_on_orders_and_exceptions(self):
		for dt in ("WC COD Parcel", "WC COD Exception"):
			self.assertTrue(self.can(dt, "CODSync WooCommerce Operator", "read"), dt)
			self.assertTrue(self.can(dt, "CODSync WooCommerce Operator", "write"), dt)

	def test_managers_can_edit_couriers_and_rates(self):
		for dt in ("WC Courier", "WC Currency Rate"):
			self.assertTrue(self.can(dt, "CODSync WooCommerce Manager", "write"), dt)

	def test_store_connection_is_for_managers_only(self):
		self.assertEqual(
			self.roles_with("WC Store Connection", "read"), {"CODSync WooCommerce Manager", "System Manager"}
		)
		self.assertEqual(
			self.roles_with("WC Store Connection", "write"), {"CODSync WooCommerce Manager", "System Manager"}
		)

	def test_sync_log_is_hidden_from_operators_but_not_viewers(self):
		self.assertTrue(self.can("WC Sync Log", "CODSync WooCommerce Viewer", "read"))
		self.assertFalse(self.can("WC Sync Log", "CODSync WooCommerce Operator", "read"))
		self.assertEqual(self.roles_with("WC Sync Log", "write") - {"System Manager"}, set())

	def test_every_codsync_role_exists(self):
		for role in (
			"CODSync WooCommerce Manager",
			"CODSync WooCommerce Operator",
			"CODSync WooCommerce Viewer",
		):
			self.assertTrue(frappe.db.exists("Role", role), role)
