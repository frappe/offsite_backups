# Copyright (c) 2015, Frappe Technologies and contributors
# License: MIT. See LICENSE

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import dropbox
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, encode, get_backups_path, get_files_path, get_request_site_address
from frappe.utils.background_jobs import enqueue
from frappe.utils.backups import new_backup
from rq.timeouts import JobTimeoutException

from offsite_backups.offsite_backups.offsite_backup_utils import (
	get_chunk_site,
	get_latest_backup_file,
	send_email,
	validate_file_size,
)

ignore_list = [".DS_Store"]
DROPBOX_CONTENT_BLOCK_SIZE = 4 * 1024 * 1024
BACKUP_JOB_ID = "offsite-backups-dropbox"
BACKUP_QUEUE = "backup"
BACKUP_TIMEOUT = 12 * 60 * 60
SYNC_STATE_FILENAME = "dropbox-sync-state.json"
BACKUP_STATUS_FILENAME = "dropbox-backup-status.json"


class DropboxSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		app_access_key: DF.Data | None
		app_secret_key: DF.Password | None
		backup_frequency: DF.Literal["", "Daily", "Weekly"]
		dropbox_access_token: DF.Password | None
		dropbox_refresh_token: DF.Password | None
		enabled: DF.Check
		file_backup: DF.Check
		limit_no_of_backups: DF.Check
		no_of_backups: DF.Int
		send_email_for_successful_backup: DF.Check
		send_notifications_to: DF.Data
	# end: auto-generated types

	def onload(self):
		if not self.app_access_key and frappe.conf.dropbox_access_key:
			self.set_onload("dropbox_setup_via_site_config", 1)

	def validate(self):
		if self.enabled and self.limit_no_of_backups and self.no_of_backups < 1:
			frappe.throw(_("Number of DB backups cannot be less than 1"))


@frappe.whitelist()
def take_backup():
	"""Enqueue a Dropbox backup on its dedicated queue."""
	enqueue_dropbox_backup()
	frappe.msgprint(_("Queued for backup. It may take several hours while files are reconciled."))


def enqueue_dropbox_backup():
	"""Enqueue one deduplicated Dropbox backup job."""
	return enqueue(
		"offsite_backups.offsite_backups.doctype.dropbox_settings.dropbox_settings.take_backup_to_dropbox",
		queue=BACKUP_QUEUE,
		timeout=BACKUP_TIMEOUT,
		job_id=f"{frappe.local.site}-{BACKUP_JOB_ID}",
		deduplicate=True,
	)


def take_backups_daily():
	take_backups_if("Daily")


def take_backups_weekly():
	take_backups_if("Weekly")


def take_backups_if(freq):
	if frappe.db.get_single_value("Dropbox Settings", "backup_frequency") == freq:
		enqueue_dropbox_backup()


def take_backup_to_dropbox(retry_count=0, upload_db_backup=True):
	did_not_upload, error_log = [], []
	write_backup_status("running", upload_db_backup=upload_db_backup)
	try:
		if not cint(frappe.db.get_single_value("Dropbox Settings", "enabled")):
			write_backup_status("disabled", upload_db_backup=upload_db_backup)
			return

		validate_file_size()
		did_not_upload, error_log, summary = backup_to_dropbox(upload_db_backup)
		if did_not_upload:
			raise RuntimeError(_("Dropbox did not accept {0} files").format(len(did_not_upload)))

		write_backup_status("success", upload_db_backup=upload_db_backup, summary=summary)

		if cint(frappe.db.get_single_value("Dropbox Settings", "send_email_for_successful_backup")):
			send_email(True, "Dropbox", "Dropbox Settings", "send_notifications_to")
	except JobTimeoutException:
		if retry_count < 2:
			args = {
				"retry_count": retry_count + 1,
				"upload_db_backup": False,  # considering till worker timeout db backup is uploaded
			}
			enqueue(
				"offsite_backups.offsite_backups.doctype.dropbox_settings.dropbox_settings.take_backup_to_dropbox",
				queue=BACKUP_QUEUE,
				timeout=BACKUP_TIMEOUT,
				**args,
			)
		write_backup_status("failed", upload_db_backup=upload_db_backup, error="Dropbox job timed out")
		raise
	except Exception as exc:
		if isinstance(error_log, str):
			error_message = error_log + "\n" + frappe.get_traceback()
		else:
			file_and_error = [" - ".join(f) for f in zip(did_not_upload, error_log, strict=False)]
			error_message = "\n".join(file_and_error) + "\n" + frappe.get_traceback()

		write_backup_status(
			"failed",
			upload_db_backup=upload_db_backup,
			error=str(exc),
			failed_files=did_not_upload,
		)
		send_email(False, "Dropbox", "Dropbox Settings", "send_notifications_to", error_message)
		raise


def backup_to_dropbox(upload_db_backup=True):
	# upload database
	dropbox_settings = get_dropbox_settings()
	dropbox_client = get_dropbox_client(dropbox_settings)

	if upload_db_backup:
		if frappe.flags.create_new_backup:
			backup = new_backup(ignore_files=True)
			filename = os.path.join(get_backups_path(), os.path.basename(backup.backup_path_db))
			site_config = os.path.join(get_backups_path(), os.path.basename(backup.backup_path_conf))
		else:
			filename, site_config = get_latest_backup_file()

		upload_and_verify(filename, "/database", dropbox_client)
		upload_and_verify(site_config, "/database", dropbox_client)

		# This Dropbox root contains backups from more than one historic site. Automatic
		# retention is intentionally disabled until backups are stored below a site-specific
		# prefix; deleting by filename from the shared folder could remove another site's DR copy.

	# upload files to files folder
	did_not_upload = []
	error_log = []

	summary = {"folders": {}}
	if dropbox_settings["file_backup"]:
		state = load_sync_state()
		for path, dropbox_folder in (
			(get_files_path(), "/files"),
			(get_files_path(is_private=1), "/private/files"),
		):
			folder_summary = reconcile_folder(
				path,
				dropbox_folder,
				dropbox_client,
				state,
				did_not_upload,
				error_log,
			)
			summary["folders"][dropbox_folder] = folder_summary
			save_sync_state(state)

	return did_not_upload, error_log, summary


def reconcile_folder(path, dropbox_folder, dropbox_client, state, did_not_upload, error_log):
	"""Reconcile regular local files against Dropbox size and content hashes."""
	path = Path(path)
	if not path.exists():
		return {"local_files": 0, "remote_files": 0, "uploaded_files": 0, "uploaded_bytes": 0}

	remote_files = {
		entry.name: entry
		for entry in get_uploaded_files_meta(dropbox_folder, dropbox_client)
		if isinstance(entry, dropbox.files.FileMetadata)
	}
	local_files = sorted(
		entry
		for entry in path.iterdir()
		if entry.name not in ignore_list and entry.is_file() and not entry.is_symlink()
	)
	uploaded_files = 0
	uploaded_bytes = 0
	verified_files = 0
	initial_failure_count = len(did_not_upload)

	for index, filepath in enumerate(local_files, start=1):
		try:
			stat = filepath.stat()
			state_key = f"{dropbox_folder}/{filepath.name}"
			local_hash = get_local_content_hash(filepath, state_key, stat, state)
			remote = remote_files.get(filepath.name)
			if remote and int(remote.size) == stat.st_size and remote.content_hash == local_hash:
				verified_files += 1
				continue

			upload_and_verify(filepath, dropbox_folder, dropbox_client, expected_hash=local_hash)
			uploaded_files += 1
			uploaded_bytes += stat.st_size
		except Exception:
			did_not_upload.append(str(filepath))
			error_log.append(frappe.get_traceback())

		if index % 100 == 0:
			save_sync_state(state)

	return {
		"local_files": len(local_files),
		"remote_files": len(remote_files),
		"verified_files": verified_files,
		"uploaded_files": uploaded_files,
		"uploaded_bytes": uploaded_bytes,
		"failed_files": len(did_not_upload) - initial_failure_count,
	}


def get_local_content_hash(filepath, state_key, stat, state):
	"""Return a cached Dropbox content hash for an unchanged local file."""
	cached = state["files"].get(state_key)
	if cached and cached.get("size") == stat.st_size and cached.get("mtime_ns") == stat.st_mtime_ns:
		return cached["content_hash"]

	content_hash = dropbox_content_hash(filepath)
	state["files"][state_key] = {
		"size": stat.st_size,
		"mtime_ns": stat.st_mtime_ns,
		"content_hash": content_hash,
	}
	return content_hash


def dropbox_content_hash(filepath):
	"""Calculate the block-composed content hash defined by Dropbox."""
	overall = hashlib.sha256()
	with open(encode(str(filepath)), "rb") as source:
		while block := source.read(DROPBOX_CONTENT_BLOCK_SIZE):
			overall.update(hashlib.sha256(block).digest())
	return overall.hexdigest()


def upload_and_verify(filename, folder, dropbox_client, expected_hash=None):
	"""Upload one file and verify Dropbox returned its exact size and hash."""
	filename = Path(filename)
	expected_hash = expected_hash or dropbox_content_hash(filename)
	metadata = upload_file_to_dropbox(str(filename), folder, dropbox_client)
	if not metadata or int(metadata.size) != filename.stat().st_size or metadata.content_hash != expected_hash:
		raise RuntimeError(f"Dropbox content verification failed for {filename.name}")
	return metadata


def upload_file_to_dropbox(filename, folder, dropbox_client):
	"""Upload a file in chunks and return Dropbox metadata."""
	if not os.path.exists(filename):
		raise FileNotFoundError("Backup source file does not exist")

	create_folder_if_not_exists(folder, dropbox_client)
	file_size = os.path.getsize(encode(filename))
	chunk_size = get_chunk_site(file_size)

	mode = dropbox.files.WriteMode.overwrite

	path = f"{folder}/{os.path.basename(filename)}"

	try:
		with open(encode(filename), "rb") as f:
			if file_size <= chunk_size:
				return dropbox_client.files_upload(f.read(), path, mode)
			else:
				upload_session_start_result = dropbox_client.files_upload_session_start(f.read(chunk_size))
				cursor = dropbox.files.UploadSessionCursor(
					session_id=upload_session_start_result.session_id, offset=f.tell()
				)
				commit = dropbox.files.CommitInfo(path=path, mode=mode)

				while f.tell() < file_size:
					if (file_size - f.tell()) <= chunk_size:
						return dropbox_client.files_upload_session_finish(f.read(chunk_size), cursor, commit)
					else:
						dropbox_client.files_upload_session_append(
							f.read(chunk_size), cursor.session_id, cursor.offset
						)
						cursor.offset = f.tell()
	except dropbox.exceptions.ApiError as e:
		if isinstance(e.error, dropbox.files.UploadError):
			error = f"File Path: {path}\n"
			error += frappe.get_traceback()
			frappe.log_error(error)
		raise


def create_folder_if_not_exists(folder, dropbox_client):
	try:
		dropbox_client.files_get_metadata(folder)
	except dropbox.exceptions.ApiError as e:
		# folder not found
		if isinstance(e.error, dropbox.files.GetMetadataError):
			dropbox_client.files_create_folder(folder)
		else:
			raise


def get_uploaded_files_meta(dropbox_folder, dropbox_client):
	try:
		response = dropbox_client.files_list_folder(dropbox_folder)
		entries = list(response.entries)
		while response.has_more:
			response = dropbox_client.files_list_folder_continue(response.cursor)
			entries.extend(response.entries)
		return entries
	except dropbox.exceptions.ApiError as e:
		# folder not found
		if isinstance(e.error, dropbox.files.ListFolderError):
			return []
		raise


def load_sync_state():
	"""Load resumable local Dropbox hash state."""
	path = Path(get_backups_path()) / SYNC_STATE_FILENAME
	try:
		state = json.loads(path.read_text())
		if state.get("version") == 1 and isinstance(state.get("files"), dict):
			return state
	except (OSError, ValueError, TypeError):
		pass
	return {"version": 1, "files": {}}


def save_sync_state(state):
	"""Persist resumable local Dropbox hash state atomically."""
	write_private_json(Path(get_backups_path()) / SYNC_STATE_FILENAME, state)


def write_backup_status(status, **details):
	"""Persist a machine-readable, secret-free backup result atomically."""
	payload = {
		"version": 1,
		"site": frappe.local.site,
		"status": status,
		"updated_at": datetime.now(UTC).isoformat(),
		**details,
	}
	write_private_json(Path(get_backups_path()) / BACKUP_STATUS_FILENAME, payload)


def write_private_json(path, payload):
	"""Write a root-private JSON file via atomic replacement."""
	path.parent.mkdir(parents=True, exist_ok=True)
	temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
	try:
		with open(temporary, "w", encoding="utf-8") as output:
			json.dump(payload, output, indent=2, sort_keys=True)
			output.write("\n")
		os.chmod(temporary, 0o600)
		os.replace(temporary, path)
	finally:
		if temporary.exists():
			temporary.unlink()


def get_dropbox_client(dropbox_settings):
	dropbox_client = dropbox.Dropbox(
		oauth2_access_token=dropbox_settings["access_token"],
		oauth2_refresh_token=dropbox_settings["refresh_token"],
		app_key=dropbox_settings["app_key"],
		app_secret=dropbox_settings["app_secret"],
		timeout=None,
	)

	# checking if the access token has expired
	dropbox_client.files_list_folder("")
	if dropbox_settings["access_token"] != dropbox_client._oauth2_access_token:
		set_dropbox_token(dropbox_client._oauth2_access_token)

	return dropbox_client


def get_dropbox_settings(redirect_uri=False):
	# NOTE: access token is kept for legacy dropbox apps
	settings = frappe.get_doc("Dropbox Settings")
	app_details = {
		"app_key": settings.app_access_key or frappe.conf.dropbox_access_key,
		"app_secret": settings.get_password(fieldname="app_secret_key", raise_exception=False)
		if settings.app_secret_key
		else frappe.conf.dropbox_secret_key,
		"refresh_token": settings.get_password("dropbox_refresh_token", raise_exception=False),
		"access_token": settings.get_password("dropbox_access_token", raise_exception=False),
		"file_backup": settings.file_backup,
		"no_of_backups": settings.no_of_backups if settings.limit_no_of_backups else None,
	}

	if redirect_uri:
		app_details.update(
			{
				"redirect_uri": get_request_site_address(True)
				+ "/api/method/offsite_backups.offsite_backups.doctype.dropbox_settings.dropbox_settings.dropbox_auth_finish"
			}
		)

	if not (app_details["app_key"] and app_details["app_secret"]):
		raise Exception(_("Please set Dropbox access keys in site config or doctype"))

	return app_details


def delete_older_backups(dropbox_client, folder_path, to_keep):
	res = dropbox_client.files_list_folder(path=folder_path)
	files = [f for f in res.entries if isinstance(f, dropbox.files.FileMetadata) and "sql" in f.name]

	if len(files) <= to_keep:
		return

	files.sort(key=lambda item: item.client_modified, reverse=True)
	for f in files[to_keep:]:
		dropbox_client.files_delete(os.path.join(folder_path, f.name))


@frappe.whitelist()
def get_dropbox_authorize_url():
	app_details = get_dropbox_settings(redirect_uri=True)
	dropbox_oauth_flow = dropbox.DropboxOAuth2Flow(
		consumer_key=app_details["app_key"],
		redirect_uri=app_details["redirect_uri"],
		session={},
		csrf_token_session_key="dropbox-auth-csrf-token",
		consumer_secret=app_details["app_secret"],
		token_access_type="offline",
	)

	auth_url = dropbox_oauth_flow.start()

	return {"auth_url": auth_url, "args": parse_qs(urlparse(auth_url).query)}


@frappe.whitelist()
def dropbox_auth_finish():
	app_details = get_dropbox_settings(redirect_uri=True)
	callback = frappe.form_dict
	close = '<p class="text-muted">' + _("Please close this window") + "</p>"

	if not callback.state or not callback.code:
		frappe.respond_as_web_page(
			_("Dropbox Setup"),
			_("Illegal Access Token. Please try again") + close,
			indicator_color="red",
			http_status_code=frappe.AuthenticationError.http_status_code,
		)
		return

	dropbox_oauth_flow = dropbox.DropboxOAuth2Flow(
		consumer_key=app_details["app_key"],
		redirect_uri=app_details["redirect_uri"],
		session={"dropbox-auth-csrf-token": callback.state},
		csrf_token_session_key="dropbox-auth-csrf-token",
		consumer_secret=app_details["app_secret"],
	)

	token = dropbox_oauth_flow.finish({"state": callback.state, "code": callback.code})
	set_dropbox_token(token.access_token, token.refresh_token)

	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = "/app/dropbox-settings"


def set_dropbox_token(access_token, refresh_token=None):
	# NOTE: used doc object instead of db.set_value so that password field is set properly
	dropbox_settings = frappe.get_single("Dropbox Settings")
	dropbox_settings.dropbox_access_token = access_token
	if refresh_token:
		dropbox_settings.dropbox_refresh_token = refresh_token

	dropbox_settings.save()

	frappe.db.commit()
