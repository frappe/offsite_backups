# Copyright (c) 2019, Frappe Technologies and Contributors
# License: MIT. See LICENSE
from unittest import TestCase
from unittest.mock import Mock

from frappe.tests import IntegrationTestCase

from offsite_backups.offsite_backups.doctype.dropbox_settings.dropbox_settings import (
	upload_file_to_dropbox,
)


class UnitTestDropboxSettings(TestCase):
	"""
	Unit tests for DropboxSettings.
	Use this class for testing individual functions and methods.
	"""

	def test_missing_file_is_not_silently_accepted(self):
		with self.assertRaisesRegex(FileNotFoundError, "Backup source file does not exist"):
			upload_file_to_dropbox("/path/that/does/not/exist", "/files", Mock())


class TestDropboxSettings(IntegrationTestCase):
	pass
