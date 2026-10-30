# Copyright (c) 2017, Frappe Technologies and contributors
# License: MIT. See LICENSE
import os
import os.path
from datetime import datetime

import boto3
import frappe
from botocore.config import Config
from botocore.exceptions import ClientError
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint
from frappe.utils.background_jobs import enqueue
from rq.timeouts import JobTimeoutException

from offsite_backups.offsite_backups.offsite_backup_utils import (
	generate_files_backup,
	get_latest_backup_file,
	send_email,
	validate_file_size,
)


def sanitize_backup_path(path):
	"""Sanitize folder prefix to prevent path traversal and normalize S3 key prefix."""
	if not path:
		return ""
	parts = [part for part in path.replace("\\", "/").split("/") if part and part != ".."]
	cleaned = "/".join(parts)
	if cleaned and not cleaned.endswith("/"):
		cleaned += "/"
	return cleaned


class S3BackupSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		access_key_id: DF.Data
		addressing_style: DF.Literal["auto", "path", "virtual"] | None
		backup_files: DF.Check
		backup_path: DF.Data | None
		bucket: DF.Data
		enable_backup_rotation: DF.Check
		enabled: DF.Check
		endpoint_url: DF.Data | None
		frequency: DF.Literal["Hourly", "Daily", "Weekly", "Monthly", "None"]
		notify_email: DF.Data
		region_name: DF.Data | None
		retention_count: DF.Int
		secret_access_key: DF.Password
		send_email_for_successful_backup: DF.Check
		service_provider: DF.Literal[
			"Amazon S3",
			"Oracle Cloud Infrastructure (OCI)",
			"Cloudflare R2",
			"MinIO",
			"Wasabi",
			"Backblaze B2",
			"DigitalOcean Spaces",
			"Google Cloud Storage",
			"Custom S3-Compatible",
		] | None
		verify_ssl: DF.Check
	# end: auto-generated types

	def validate(self):
		if not self.enabled:
			return

		if not self.endpoint_url:
			provider = getattr(self, "service_provider", None) or "Amazon S3"
			if provider == "Amazon S3":
				self.endpoint_url = "https://s3.amazonaws.com"

		if self.backup_path:
			self.backup_path = sanitize_backup_path(self.backup_path)

		if getattr(self, "enable_backup_rotation", False):
			if not getattr(self, "retention_count", None) or cint(self.retention_count) < 1:
				frappe.throw(_("Retention Count (number of backups to keep) must be at least 1."))

		self.test_connection()

	def test_connection(self):
		conn = get_s3_client(self)
		try:
			# Head_bucket returns a 200 OK if the bucket exists and have access to it.
			# Requires ListBucket / Bucket Read permission
			conn.head_bucket(Bucket=self.bucket)
			return {
				"status": "success",
				"message": _("Connection successful! Bucket '{0}' is accessible.").format(self.bucket),
			}
		except ClientError as e:
			error_code = str(e.response.get("Error", {}).get("Code", ""))
			error_msg = e.response.get("Error", {}).get("Message", str(e))
			bucket_name = frappe.bold(self.bucket)
			if error_code == "403":
				msg = _(
					"Access denied for bucket {0}. Check your Access Key, Secret Key, and IAM/Bucket policy permissions."
				).format(bucket_name)
			elif error_code == "404":
				msg = _("Bucket {0} not found. Ensure the bucket exists at endpoint '{1}'.").format(
					bucket_name, self.endpoint_url
				)
			elif error_code in ("301", "PermanentRedirect"):
				msg = _(
					"Bucket {0} is in a different region. Please specify the correct Region (e.g., in Region field)."
				).format(bucket_name)
			else:
				msg = _("Failed to access bucket {0}: {1} ({2})").format(bucket_name, error_msg, error_code)

			frappe.throw(msg, title=_("Object Storage Connection Failed"))
		except Exception as e:
			frappe.throw(
				_("Failed to connect to object storage: {0}").format(str(e)),
				title=_("Object Storage Connection Failed"),
			)


def get_s3_client(doc=None):
	"""
	Returns a configured boto3 S3 client supporting AWS S3 and any S3-compatible
	object storage service (Oracle Cloud Infrastructure, Cloudflare R2, MinIO,
	Wasabi, Backblaze B2, DigitalOcean Spaces, Ceph, etc.).
	"""
	if doc is None:
		doc = frappe.get_single("S3 Backup Settings")

	endpoint_url = (getattr(doc, "endpoint_url", None) or "").strip()
	provider = getattr(doc, "service_provider", None) or "Amazon S3"

	if not endpoint_url:
		endpoint_url = "https://s3.amazonaws.com"
	elif not (endpoint_url.startswith("http://") or endpoint_url.startswith("https://")):
		endpoint_url = f"https://{endpoint_url}"

	endpoint_url = endpoint_url.rstrip("/")

	region_name = (getattr(doc, "region_name", None) or "").strip() or None
	addressing_style = (getattr(doc, "addressing_style", None) or "auto").strip()

	# Oracle Cloud Infrastructure (OCI) and MinIO require path-style addressing
	if provider == "Oracle Cloud Infrastructure (OCI)" or addressing_style == "path":
		addressing_style = "path"

	s3_config = {}
	if addressing_style in ("path", "virtual"):
		s3_config["addressing_style"] = addressing_style

	config_kwargs = {
		"signature_version": "s3v4",
	}
	if s3_config:
		config_kwargs["s3"] = s3_config

	# Support modern botocore checksum settings (prevents 400 errors on OCI / 3rd party S3)
	try:
		boto_config = Config(
			request_checksum_calculation="when_required",
			response_checksum_validation="when_required",
			**config_kwargs,
		)
	except TypeError:
		boto_config = Config(**config_kwargs)

	verify_ssl = True
	if hasattr(doc, "verify_ssl") and doc.verify_ssl is not None:
		verify_ssl = bool(cint(doc.verify_ssl))

	secret_key = (
		doc.get_password("secret_access_key")
		if hasattr(doc, "get_password")
		else getattr(doc, "secret_access_key", None)
	)

	client_kwargs = {
		"service_name": "s3",
		"aws_access_key_id": doc.access_key_id,
		"aws_secret_access_key": secret_key,
		"endpoint_url": endpoint_url,
		"config": boto_config,
		"verify": verify_ssl,
	}
	if region_name:
		client_kwargs["region_name"] = region_name

	return boto3.client(**client_kwargs)


@frappe.whitelist()
def test_connection():
	"""Whitelisted method to test connection from the UI button (System Manager only)."""
	frappe.only_for("System Manager")
	doc = frappe.get_single("S3 Backup Settings")
	return doc.test_connection()


@frappe.whitelist()
def take_backup():
	"""Enqueue background job for taking backup to S3/Object Storage (System Manager only)."""
	frappe.only_for("System Manager")
	enqueue(
		"offsite_backups.offsite_backups.doctype.s3_backup_settings.s3_backup_settings.take_backups_s3",
		queue="long",
		timeout=1500,
	)
	frappe.msgprint(_("Queued for backup. It may take a few minutes to an hour."))


def take_backups_hourly():
	take_backups_if("Hourly")


def take_backups_daily():
	take_backups_if("Daily")


def take_backups_weekly():
	take_backups_if("Weekly")


def take_backups_monthly():
	take_backups_if("Monthly")


def take_backups_if(freq):
	if cint(frappe.db.get_single_value("S3 Backup Settings", "enabled")):
		if frappe.db.get_single_value("S3 Backup Settings", "frequency") == freq:
			take_backups_s3()


@frappe.whitelist()
def take_backups_s3(retry_count=0):
	# Allow execution if running via background scheduler or explicitly invoked by System Manager
	if frappe.session.user and frappe.session.user != "Administrator":
		roles = frappe.get_roles()
		if "System Manager" not in roles:
			frappe.throw(_("Not permitted"), frappe.PermissionError)

	provider = frappe.db.get_single_value("S3 Backup Settings", "service_provider") or "S3 / Object Storage"
	try:
		validate_file_size()
		backup_to_s3()
		delete_old_backups_from_s3()
		send_email(True, provider, "S3 Backup Settings", "notify_email")
	except JobTimeoutException:
		if retry_count < 2:
			args = {"retry_count": retry_count + 1}
			enqueue(
				"offsite_backups.offsite_backups.doctype.s3_backup_settings.s3_backup_settings.take_backups_s3",
				queue="long",
				timeout=1500,
				**args,
			)
		else:
			notify()
	except Exception:
		notify()


def notify():
	error_message = frappe.get_traceback()
	provider = frappe.db.get_single_value("S3 Backup Settings", "service_provider") or "S3 / Object Storage"
	send_email(False, provider, "S3 Backup Settings", "notify_email", error_message)


def backup_to_s3():
	from frappe.utils import get_backups_path
	from frappe.utils.backups import new_backup

	doc = frappe.get_single("S3 Backup Settings")
	bucket = doc.bucket
	path = sanitize_backup_path(doc.backup_path)
	backup_files = cint(doc.backup_files)

	conn = get_s3_client(doc)

	if frappe.flags.create_new_backup:
		backup = new_backup(
			ignore_files=False,
			backup_path_db=None,
			backup_path_files=None,
			backup_path_private_files=None,
			force=True,
		)
		db_filename = os.path.join(get_backups_path(), os.path.basename(backup.backup_path_db))
		site_config = os.path.join(get_backups_path(), os.path.basename(backup.backup_path_conf))
		if backup_files:
			files_filename = os.path.join(get_backups_path(), os.path.basename(backup.backup_path_files))
			private_files = os.path.join(
				get_backups_path(), os.path.basename(backup.backup_path_private_files)
			)
	else:
		if backup_files:
			db_filename, site_config, files_filename, private_files = get_latest_backup_file(
				with_files=backup_files
			)

			if not files_filename or not private_files:
				generate_files_backup()
				db_filename, site_config, files_filename, private_files = get_latest_backup_file(
					with_files=backup_files
				)

		else:
			db_filename, site_config = get_latest_backup_file()

	folder = path + os.path.basename(db_filename)[:15] + "/"
	# for adding datetime to folder name

	upload_file_to_s3(db_filename, folder, conn, bucket)
	upload_file_to_s3(site_config, folder, conn, bucket)

	if backup_files:
		if private_files:
			upload_file_to_s3(private_files, folder, conn, bucket)

		if files_filename:
			upload_file_to_s3(files_filename, folder, conn, bucket)


def upload_file_to_s3(filename, folder, conn, bucket):
	from boto3.s3.transfer import TransferConfig

	# Ensure forward slashes in S3 object key
	destpath = (folder + os.path.basename(filename)).replace("\\", "/")
	print(f"Uploading file: {filename} to {bucket}/{destpath}")

	transfer_config = TransferConfig(
		multipart_threshold=25 * 1024 * 1024,
		multipart_chunksize=25 * 1024 * 1024,
		max_concurrency=4,
		use_threads=True,
	)
	conn.upload_file(filename, bucket, destpath, Config=transfer_config)


def delete_old_backups_from_s3():
	"""Delete old backups from S3 / Object Storage based on rotation retention count."""
	doc = frappe.get_single("S3 Backup Settings")
	if (
		not doc.enabled
		or not getattr(doc, "enable_backup_rotation", False)
		or not getattr(doc, "retention_count", None)
	):
		return 0

	retention_count = cint(doc.retention_count)
	if retention_count < 1:
		return 0

	conn = get_s3_client(doc)
	bucket = doc.bucket
	path = sanitize_backup_path(doc.backup_path)

	paginator = conn.get_paginator("list_objects_v2")
	pages = paginator.paginate(Bucket=bucket, Prefix=path, Delimiter="/")

	backup_folders = []
	for page in pages:
		for prefix in page.get("CommonPrefixes", []):
			folder = prefix.get("Prefix", "")
			folder_name = folder[len(path) :]
			if len(folder_name) >= 15:
				date_str = folder_name[:15]
				try:
					folder_date = datetime.strptime(date_str, "%Y%m%d_%H%M%S")
					backup_folders.append((folder, folder_date))
				except ValueError:
					continue

	# Sort newest first
	backup_folders.sort(key=lambda x: x[1], reverse=True)

	if len(backup_folders) <= retention_count:
		return 0

	folders_to_delete = backup_folders[retention_count:]
	for folder, _ in folders_to_delete:
		delete_s3_folder(conn, bucket, folder)

	return len(folders_to_delete)


def delete_s3_folder(conn, bucket, folder):
	"""Delete all objects with the given prefix folder in batches of 1000."""
	paginator = conn.get_paginator("list_objects_v2")
	pages = paginator.paginate(Bucket=bucket, Prefix=folder)

	objects_to_delete = []
	for page in pages:
		for obj in page.get("Contents", []):
			objects_to_delete.append({"Key": obj["Key"]})

	for i in range(0, len(objects_to_delete), 1000):
		batch = objects_to_delete[i : i + 1000]
		if batch:
			conn.delete_objects(
				Bucket=bucket,
				Delete={"Objects": batch},
			)
