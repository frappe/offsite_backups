# Copyright (c) 2019, Frappe Technologies and Contributors
# License: MIT. See LICENSE
import os
import sys
import unittest
from unittest.mock import MagicMock

# Ensure repo root is on sys.path for direct test runs
repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
if repo_root not in sys.path:
	sys.path.insert(0, repo_root)

try:
	import frappe
	from frappe.tests import IntegrationTestCase, UnitTestCase
except ImportError:
	frappe = MagicMock()
	sys.modules["frappe"] = frappe
	sys.modules["frappe.model.document"] = MagicMock()
	sys.modules["frappe.utils"] = MagicMock()
	sys.modules["frappe.utils.backups"] = MagicMock()
	sys.modules["frappe.utils.background_jobs"] = MagicMock()
	sys.modules["frappe.database.utils"] = MagicMock()
	sys.modules["frappe.integrations.google_oauth"] = MagicMock()
	sys.modules["apiclient.http"] = MagicMock()
	sys.modules["googleapiclient.errors"] = MagicMock()
	sys.modules["rq"] = MagicMock()
	sys.modules["rq.timeouts"] = MagicMock()
	sys.modules["offsite_backups.offsite_backups.offsite_backup_utils"] = MagicMock()
	IntegrationTestCase = unittest.TestCase
	UnitTestCase = unittest.TestCase


class TestGoogleDrive(UnitTestCase):
	def test_oauth_domain_is_google_drive(self):
		from offsite_backups.offsite_backups.doctype.google_drive.google_drive import (
			get_google_drive_oauth_obj,
		)

		with unittest.mock.patch(
			"offsite_backups.offsite_backups.doctype.google_drive.google_drive.GoogleOAuth"
		) as mock_oauth:
			get_google_drive_oauth_obj()
			mock_oauth.assert_called_once()
			args, kwargs = mock_oauth.call_args
			# Ensure domain is "google_drive" (which Frappe callback whitelist allows)
			self.assertEqual(args[0], "google_drive")


if __name__ == "__main__":
	unittest.main()
