# Copyright (c) 2019, Frappe Technologies and Contributors
# License: MIT. See LICENSE
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

import dropbox
from frappe.tests import IntegrationTestCase

from offsite_backups.offsite_backups.doctype.dropbox_settings.dropbox_settings import (
	dropbox_content_hash,
	get_uploaded_files_meta,
	reconcile_folder,
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

	def test_content_hash_uses_dropbox_block_composition(self):
		with tempfile.NamedTemporaryFile() as source:
			source.write(b"backup contents")
			source.flush()

			self.assertEqual(
				dropbox_content_hash(source.name),
				"af5e8931ebc7dddda299f4b7df6f036783ed77a8fa77c2fd2536e5abf45ed468",
			)

	def test_folder_reconciliation_uses_filesystem_not_file_rows(self):
		with tempfile.TemporaryDirectory() as folder:
			local_file = Path(folder) / "attachment.bin"
			local_file.write_bytes(b"customer attachment")
			state = {"version": 1, "files": {}}
			failed = []
			errors = []

			with (
				patch(
					"offsite_backups.offsite_backups.doctype.dropbox_settings.dropbox_settings.get_uploaded_files_meta",
					return_value=[],
				),
				patch(
					"offsite_backups.offsite_backups.doctype.dropbox_settings.dropbox_settings.upload_and_verify"
				) as upload,
				patch(
					"offsite_backups.offsite_backups.doctype.dropbox_settings.dropbox_settings.save_sync_state"
				),
			):
				summary = reconcile_folder(folder, "/private/files", Mock(), state, failed, errors)

			upload.assert_called_once()
			self.assertEqual(summary["local_files"], 1)
			self.assertEqual(summary["uploaded_files"], 1)
			self.assertFalse(failed)

	def test_remote_listing_follows_all_pages(self):
		client = Mock()
		client.files_list_folder.return_value = Mock(entries=["one"], has_more=True, cursor="next")
		client.files_list_folder_continue.return_value = Mock(entries=["two"], has_more=False)

		self.assertEqual(get_uploaded_files_meta("/files", client), ["one", "two"])
		client.files_list_folder_continue.assert_called_once_with("next")


class TestDropboxSettings(IntegrationTestCase):
	pass
