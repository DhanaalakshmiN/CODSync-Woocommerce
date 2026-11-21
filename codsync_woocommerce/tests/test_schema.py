"""Check installable schema ownership and independence before any site is touched."""

import ast
import importlib
import json
import unittest
from pathlib import Path

from frappe.model.document import Document

from codsync_woocommerce import hooks
from codsync_woocommerce.install import ROLES

ROOT = Path(__file__).resolve().parents[1]


class TestIndependentSchema(unittest.TestCase):
	def test_controllers_match_owned_doctypes(self):
		for path in (ROOT / "codsync_woocommerce/doctype").glob("*/*.json"):
			with self.subTest(doctype=path.parent.name):
				meta = json.loads(path.read_text())
				self.assertTrue(meta["name"].startswith("WC "))
				self.assertEqual(meta["module"], "CODSync WooCommerce")
				module = importlib.import_module(
					f"codsync_woocommerce.codsync_woocommerce.doctype.{path.parent.name}.{path.stem}"
				)
				cls = getattr(module, meta["name"].replace(" ", ""))
				self.assertTrue(issubclass(cls, Document))

	def test_schema_links_cannot_point_to_shopify_records(self):
		paths = list((ROOT / "codsync_woocommerce/doctype").glob("*/*.json"))
		owned = {json.loads(path.read_text())["name"] for path in paths}
		for path in paths:
			meta = json.loads(path.read_text())
			fields = {row["fieldname"]: row for row in meta["fields"]}
			self.assertTrue(set(meta["field_order"]) <= fields.keys())
			for row in fields.values():
				if row["fieldtype"] in ("Link", "Table"):
					self.assertIn(row["options"], owned | {"User", "Currency", "DocType"})
			for permission in meta.get("permissions", []):
				self.assertIn(permission["role"], set(ROLES) | {"System Manager"})

	def test_connection_stores_only_woocommerce_credentials(self):
		meta = json.loads(
			(ROOT / "codsync_woocommerce/doctype/wc_store_connection/wc_store_connection.json").read_text()
		)
		fields = {row["fieldname"]: row for row in meta["fields"]}
		for name in ("woo_consumer_key", "woo_consumer_secret", "webhook_secret"):
			self.assertEqual(fields[name]["fieldtype"], "Password")
		self.assertFalse(
			{"access_token", "token_expiry", "shopify_client_id", "shopify_client_secret"} & fields.keys()
		)
		self.assertEqual(fields["platform"]["options"], "WooCommerce")

	def test_scheduler_paths_resolve_within_this_app(self):
		for jobs in hooks.scheduler_events.values():
			for name in jobs:
				self.assertTrue(name.startswith("codsync_woocommerce."))
				module, function = name.rsplit(".", 1)
				self.assertTrue(callable(getattr(importlib.import_module(module), function)))

	def test_no_python_import_depends_on_codsync(self):
		for path in ROOT.rglob("*.py"):
			for node in ast.walk(ast.parse(path.read_text())):
				if isinstance(node, ast.ImportFrom):
					self.assertNotEqual((node.module or "").split(".")[0], "codsync", str(path))
				elif isinstance(node, ast.Import):
					self.assertFalse(
						any(alias.name.split(".")[0] == "codsync" for alias in node.names), str(path)
					)

	def test_reports_reference_only_owned_doctypes(self):
		for path in (ROOT / "codsync_woocommerce/report").glob("*/*.json"):
			meta = json.loads(path.read_text())
			self.assertTrue(meta["name"].startswith("WC "))
			self.assertTrue(meta["ref_doctype"].startswith("WC "))
			module = importlib.import_module(
				f"codsync_woocommerce.codsync_woocommerce.report.{path.parent.name}.{path.stem}"
			)
			self.assertTrue(callable(module.execute))
