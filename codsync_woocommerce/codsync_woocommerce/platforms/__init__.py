"""WooCommerce is the only store integration in this independent app."""

import frappe
from frappe import _

from codsync_woocommerce.codsync_woocommerce.platforms.woocommerce import WooCommercePlatform

PLATFORMS = {"WooCommerce": WooCommercePlatform}


def get_platform(name=None):
	if name and name not in PLATFORMS:
		frappe.throw(_("Unknown platform {0}").format(name))
	return WooCommercePlatform()
