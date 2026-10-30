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
	sys.modules["rq"] = MagicMock()
	sys.modules["rq.timeouts"] = MagicMock()
	sys.modules["offsite_backups.offsite_backups.offsite_backup_utils"] = MagicMock()
	IntegrationTestCase = unittest.TestCase
	UnitTestCase = unittest.TestCase

try:
	import dropbox
except ImportError:
	dropbox = MagicMock()
	sys.modules["dropbox"] = dropbox


class MockFileMetadata:
	def __init__(self, name, client_modified):
		self.name = name
		self.client_modified = client_modified


class TestDropboxSettings(UnitTestCase):
	def test_delete_older_backups_cleans_sql_and_json(self):
		from offsite_backups.offsite_backups.doctype.dropbox_settings.dropbox_settings import (
			delete_older_backups,
		)

		mock_client = MagicMock()
		# Provide mock list of files
		f1 = MockFileMetadata("20261030_120000-site-database.sql.gz", 3)
		f2 = MockFileMetadata("20261030_120000-site-site_config.json", 3)
		f3 = MockFileMetadata("20261029_120000-site-database.sql.gz", 2)
		f4 = MockFileMetadata("20261029_120000-site-site_config.json", 2)
		f5 = MockFileMetadata("20261028_120000-site-database.sql.gz", 1)
		f6 = MockFileMetadata("20261028_120000-site-site_config.json", 1)

		entries = [f1, f2, f3, f4, f5, f6]
		dropbox.files.FileMetadata = MockFileMetadata
		mock_client.files_list_folder.return_value = MagicMock(entries=entries)

		# Keep only 2 backups
		delete_older_backups(mock_client, "/database", 2)

		# The oldest backup (20261028_120000) should have both .sql.gz and .json deleted
		deleted_paths = [call[0][0] for call in mock_client.files_delete.call_args_list]
		self.assertIn("/database/20261028_120000-site-database.sql.gz", deleted_paths)
		self.assertIn("/database/20261028_120000-site-site_config.json", deleted_paths)
		self.assertNotIn("/database/20261030_120000-site-database.sql.gz", deleted_paths)
		self.assertNotIn("/database/20261029_120000-site-database.sql.gz", deleted_paths)


if __name__ == "__main__":
	unittest.main()
