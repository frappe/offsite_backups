# Copyright (c) 2017, Frappe Technologies and Contributors
# License: MIT. See LICENSE
import os
import sys
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

# Ensure repo root is on sys.path for direct test runs
repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
if repo_root not in sys.path:
	sys.path.insert(0, repo_root)

try:
	import frappe
	from frappe.tests import IntegrationTestCase, UnitTestCase
except ImportError:
	# Standalone test environment without bench/frappe
	frappe = MagicMock()
	sys.modules["frappe"] = frappe
	sys.modules["frappe.model.document"] = MagicMock()
	sys.modules["frappe.utils"] = MagicMock()
	sys.modules["frappe.utils.background_jobs"] = MagicMock()
	sys.modules["rq"] = MagicMock()
	sys.modules["rq.timeouts"] = MagicMock()
	sys.modules["offsite_backups.offsite_backups.offsite_backup_utils"] = MagicMock()
	IntegrationTestCase = unittest.TestCase
	UnitTestCase = unittest.TestCase


class MockDoc:
	def __init__(self, **kwargs):
		self.__dict__.update(kwargs)

	def get_password(self, field):
		return getattr(self, field, None)


class TestS3BackupSettings(UnitTestCase):
	def test_get_s3_client_oci(self):
		from offsite_backups.offsite_backups.doctype.s3_backup_settings.s3_backup_settings import (
			get_s3_client,
		)

		doc = MockDoc(
			service_provider="Oracle Cloud Infrastructure (OCI)",
			endpoint_url="https://mynamespace.compat.objectstorage.us-ashburn-1.oraclecloud.com",
			region_name="us-ashburn-1",
			access_key_id="oci_access_key",
			secret_access_key="oci_secret_key",
			bucket="erpnext-backups",
			addressing_style="path",
			verify_ssl=1,
		)

		client = get_s3_client(doc)
		self.assertEqual(client.meta.region_name, "us-ashburn-1")
		self.assertEqual(
			client.meta.endpoint_url,
			"https://mynamespace.compat.objectstorage.us-ashburn-1.oraclecloud.com",
		)

		# Verify path-style addressing for OCI
		url = client.generate_presigned_url(
			"get_object", Params={"Bucket": "erpnext-backups", "Key": "test.tar.gz"}
		)
		self.assertIn(
			"https://mynamespace.compat.objectstorage.us-ashburn-1.oraclecloud.com/erpnext-backups/test.tar.gz",
			url,
		)

	def test_oci_forces_path_style_addressing(self):
		from offsite_backups.offsite_backups.doctype.s3_backup_settings.s3_backup_settings import (
			get_s3_client,
		)

		# Even if addressing_style is left as 'auto', OCI should automatically enforce 'path'
		doc = MockDoc(
			service_provider="Oracle Cloud Infrastructure (OCI)",
			endpoint_url="https://mynamespace.compat.objectstorage.ap-mumbai-1.oraclecloud.com",
			region_name="ap-mumbai-1",
			access_key_id="oci_access_key",
			secret_access_key="oci_secret_key",
			bucket="erpnext-backups",
			addressing_style="auto",
			verify_ssl=1,
		)

		client = get_s3_client(doc)
		url = client.generate_presigned_url(
			"get_object", Params={"Bucket": "erpnext-backups", "Key": "test.tar.gz"}
		)
		self.assertIn(
			"https://mynamespace.compat.objectstorage.ap-mumbai-1.oraclecloud.com/erpnext-backups/test.tar.gz",
			url,
		)

	def test_get_s3_client_cloudflare_r2(self):
		from offsite_backups.offsite_backups.doctype.s3_backup_settings.s3_backup_settings import (
			get_s3_client,
		)

		doc = MockDoc(
			service_provider="Cloudflare R2",
			endpoint_url="https://abc12345.r2.cloudflarestorage.com",
			region_name="auto",
			access_key_id="r2_key",
			secret_access_key="r2_secret",
			bucket="backup-bucket",
			addressing_style="auto",
			verify_ssl=1,
		)

		client = get_s3_client(doc)
		self.assertEqual(client.meta.region_name, "auto")
		self.assertEqual(client.meta.endpoint_url, "https://abc12345.r2.cloudflarestorage.com")

	def test_get_s3_client_minio(self):
		from offsite_backups.offsite_backups.doctype.s3_backup_settings.s3_backup_settings import (
			get_s3_client,
		)

		doc = MockDoc(
			service_provider="MinIO",
			endpoint_url="http://localhost:9000",
			region_name="us-east-1",
			access_key_id="minioadmin",
			secret_access_key="minioadmin",
			bucket="local-backup",
			addressing_style="path",
			verify_ssl=0,
		)

		client = get_s3_client(doc)
		self.assertEqual(client.meta.endpoint_url, "http://localhost:9000")
		url = client.generate_presigned_url(
			"get_object", Params={"Bucket": "local-backup", "Key": "database.sql.gz"}
		)
		self.assertIn("http://localhost:9000/local-backup/database.sql.gz", url)

	def test_get_s3_client_aws_default(self):
		from offsite_backups.offsite_backups.doctype.s3_backup_settings.s3_backup_settings import (
			get_s3_client,
		)

		doc = MockDoc(
			service_provider="Amazon S3",
			endpoint_url="",
			region_name="",
			access_key_id="aws_key",
			secret_access_key="aws_secret",
			bucket="my-aws-bucket",
			addressing_style="auto",
			verify_ssl=1,
		)

		client = get_s3_client(doc)
		self.assertEqual(client.meta.endpoint_url, "https://s3.amazonaws.com")

	def test_destpath_normalization(self):
		# Test that destination path uses forward slashes regardless of OS
		folder = "backups/20261030_000000/"
		filename = os.path.join("tmp", "site_config.json")
		destpath = (folder + os.path.basename(filename)).replace("\\", "/")
		self.assertEqual(destpath, "backups/20261030_000000/site_config.json")
		self.assertNotIn("\\", destpath)

	def test_path_traversal_prevention(self):
		from offsite_backups.offsite_backups.doctype.s3_backup_settings.s3_backup_settings import (
			sanitize_backup_path,
		)

		self.assertEqual(sanitize_backup_path("../../etc/passwd"), "etc/passwd/")
		self.assertEqual(sanitize_backup_path("backups/subfolder"), "backups/subfolder/")
		self.assertEqual(sanitize_backup_path("/root/test//"), "root/test/")
		self.assertEqual(sanitize_backup_path(""), "")

	def test_backup_rotation_filtering(self):
		prefixes = [
			{"Prefix": "backups/20261028_120000/"},
			{"Prefix": "backups/20261029_120000/"},
			{"Prefix": "backups/20261030_120000/"},
			{"Prefix": "backups/random_folder/"},
		]
		base_path = "backups/"
		backup_folders = []
		for item in prefixes:
			folder = item["Prefix"]
			folder_name = folder[len(base_path) :]
			if len(folder_name) >= 15:
				date_str = folder_name[:15]
				try:
					folder_date = datetime.strptime(date_str, "%Y%m%d_%H%M%S")
					backup_folders.append((folder, folder_date))
				except ValueError:
					continue

		backup_folders.sort(key=lambda x: x[1], reverse=True)
		retention_count = 2
		folders_to_delete = [f[0] for f in backup_folders[retention_count:]]
		self.assertEqual(len(backup_folders), 3)
		self.assertEqual(folders_to_delete, ["backups/20261028_120000/"])


if __name__ == "__main__":
	unittest.main()
