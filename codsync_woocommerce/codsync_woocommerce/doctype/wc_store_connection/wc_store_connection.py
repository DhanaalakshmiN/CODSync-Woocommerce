import frappe
from frappe.model.document import Document

from codsync_woocommerce.codsync_woocommerce.platforms import get_platform


class WCStoreConnection(Document):
	@frappe.whitelist(methods=["POST"])
	def test_connection(self):
		frappe.only_for(("System Manager", "CODSync WooCommerce Manager"))
		try:
			ok, message = get_platform(self.platform).test_connection(self)
		except NotImplementedError as e:
			ok, message = False, str(e)
		return {"ok": ok, "message": message, "platform": self.platform}

	@frappe.whitelist(methods=["POST"])
	def fetch_orders(self):
		frappe.only_for(("System Manager", "CODSync WooCommerce Manager"))
		try:
			return get_platform(self.platform).fetch_orders(self)
		except NotImplementedError as e:
			frappe.throw(str(e))

	@frappe.whitelist(methods=["POST"])
	def recalculate_currency_amounts(self):
		frappe.only_for(("System Manager", "CODSync WooCommerce Manager"))
		from codsync_woocommerce.codsync_woocommerce.engine import recalculate_currency_amounts

		return recalculate_currency_amounts()

	def validate(self):
		from codsync_woocommerce.codsync_woocommerce.currency import default_base_currency
		from codsync_woocommerce.codsync_woocommerce.platforms.base import normalize_store_url

		if self.platform and self.platform != "WooCommerce":
			frappe.throw("This app supports WooCommerce only")
		self.platform = "WooCommerce"
		if self.store_url:
			try:
				self.store_url = normalize_store_url(self.store_url)
			except ValueError as error:
				frappe.throw(str(error))
		self.woo_api_version = "wc/v3"
		self.base_currency = (self.base_currency or default_base_currency()).upper()
