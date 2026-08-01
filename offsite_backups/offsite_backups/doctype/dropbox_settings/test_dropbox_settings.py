# Copyright (c) 2019, Frappe Technologies and Contributors
# License: MIT. See LICENSE
import tempfile
from unittest import TestCase
from unittest.mock import Mock, patch

import dropbox
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

	def test_dropbox_upload_error_is_not_silently_accepted(self):
		dropbox_client = Mock()
		upload_error = dropbox.exceptions.ApiError(
			"request-id",
			dropbox.files.UploadError.content_hash_mismatch,
			"content hash mismatch",
			"en",
		)
		dropbox_client.files_upload.side_effect = upload_error

		with tempfile.NamedTemporaryFile() as backup_file:
			backup_file.write(b"backup contents")
			backup_file.flush()
			with (
				patch("frappe.log_error") as log_error,
				self.assertRaises(dropbox.exceptions.ApiError) as raised,
			):
				upload_file_to_dropbox(backup_file.name, "/files", dropbox_client)

		self.assertIs(raised.exception, upload_error)
		log_error.assert_called_once()


class TestDropboxSettings(IntegrationTestCase):
	pass
