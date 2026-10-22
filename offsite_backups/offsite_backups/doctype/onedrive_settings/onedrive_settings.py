# OneDrive Settings - Revision 1 - 2026-10-22 09:10:00
# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE
"""OneDrive and SharePoint backup settings for Frappe.

The desk form stores an Azure app registration and a Microsoft refresh token.
Scheduled jobs upload the standard Frappe backup archives through Microsoft Graph.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar
from urllib.parse import quote

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import (
	cint,
	get_backups_path,
	get_request_site_address,
	now_datetime,
	split_emails,
	validate_email_address,
)
from frappe.utils.background_jobs import enqueue
from frappe.utils.backups import new_backup
from frappe.utils.password import set_encrypted_password
from rq.timeouts import JobTimeoutException

from offsite_backups.offsite_backups.doctype.onedrive_settings.onedrive_graph import (
	CALLBACK_PATH,
	GraphAuthError,
	GraphClient,
	GraphError,
	MicrosoftOAuth,
	SettingsInputError,
	UrllibTransport,
	assert_backup_filename,
	backup_group_key,
	build_authorize_url,
	confine_to_backups,
	create_pkce_pair,
	delegated_scopes,
	finish_cache_key,
	is_oauth_state,
	new_oauth_state,
	parse_sharepoint_site_url,
	sanitize_folder_path,
	select_document_library,
	select_items_to_delete,
	state_cache_key,
	validate_client_id,
	validate_client_secret,
	validate_redirect_uri,
	validate_tenant,
)
from offsite_backups.offsite_backups.offsite_backup_utils import (
	generate_files_backup,
	get_latest_backup_file,
	send_email,
	validate_file_size,
)

UPLOAD_METHOD = (
	"offsite_backups.offsite_backups.doctype.onedrive_settings."
	"onedrive_settings.upload_system_backup_to_onedrive"
)
SERVICE_NAME = "OneDrive / SharePoint"
DOCTYPE_NAME = "OneDrive Settings"
OAUTH_STATE_TTL_SECONDS = 600
PASSWORD_PLACEHOLDER = "**********"
_ActionResult = TypeVar("_ActionResult")
_SECRET_QUERY = re.compile(r"(?i)(access_token|refresh_token|client_secret|tempauth|code_verifier)=([^&\s]+)")
_URL_TEXT = re.compile(r"https://\S+")


class OneDriveSettings(Document):
	"""
	Single settings document for backups to OneDrive or a SharePoint library.
	"""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		authorized_user: DF.Data | None
		backup_folder_id: DF.Data | None
		backup_folder_name: DF.Data
		backup_target: DF.Literal["", "OneDrive", "SharePoint"]
		client_id: DF.Data | None
		client_secret: DF.Password | None
		document_library: DF.Data | None
		drive_id: DF.Data | None
		enable: DF.Check
		file_backup: DF.Check
		frequency: DF.Literal["", "Daily", "Weekly", "Monthly"]
		last_backup_on: DF.Datetime | None
		limit_no_of_backups: DF.Check
		no_of_backups: DF.Int
		refresh_token: DF.Password | None
		send_email_for_successful_backup: DF.Check
		send_notification_to: DF.Data
		sharepoint_site_url: DF.Data | None
		site_id: DF.Data | None
		tenant_id: DF.Data | None
	# end: auto-generated types

	def onload(self) -> None:
		"""
		Tell the form when credentials come from site config and show the redirect URI.
		"""
		client_id = frappe.conf.get("onedrive_client_id")
		client_secret = frappe.conf.get("onedrive_client_secret")
		tenant_id = frappe.conf.get("onedrive_tenant_id")
		if _configured(client_id) and _configured(client_secret):
			self.set_onload("credentials_from_site_config", 1)
		if _configured(tenant_id):
			self.set_onload("tenant_from_site_config", 1)
		try:
			self.set_onload("redirect_uri", get_redirect_uri())
		except Exception:
			self.set_onload("redirect_error", 1)

	def validate(self) -> None:
		"""
		Check an enabled backup and forget cached ids when the destination changes.
		"""
		previous = self.get_doc_before_save()
		if previous and _destination_changed(previous, self):
			self.backup_folder_id = ""
			self.drive_id = ""
			self.site_id = ""
		if previous and _app_changed(previous, self):
			self.refresh_token = ""
			self.authorized_user = ""
		if not self.enable:
			return
		try:
			_validate_enabled(self)
		except SettingsInputError as error:
			frappe.throw(describe_input_error(error))


@dataclass(frozen=True)
class DriveLocation:
	"""
	Resolved drive and backup folder.
	"""

	drive_id: str
	folder_id: str
	site_id: str


class GraphSession:
	"""
	Refresh a Microsoft token and retry once when Graph rejects it.
	"""

	def __init__(self, settings: OneDriveSettings) -> None:
		"""
		Connect with the refresh token stored on the settings document.

		:param settings: Saved OneDrive Settings.
		"""
		self.settings = settings
		self.client = self.connect()

	def connect(self) -> GraphClient:
		"""
		Refresh the access token and keep a rotated refresh token.

		:returns: Graph client for this upload.
		"""
		credentials = app_credentials(self.settings)
		refresh_token = self.settings.get_password("refresh_token", raise_exception=False) or ""
		if not refresh_token:
			raise SettingsInputError("missing_refresh_token")
		tokens = MicrosoftOAuth(
			UrllibTransport(),
			tenant=credentials["tenant"],
			client_id=credentials["client_id"],
			client_secret=credentials["client_secret"],
			redirect_uri="",
			scopes=delegated_scopes(self.settings.backup_target),
		).refresh(refresh_token)
		if tokens.refresh_token and tokens.refresh_token != refresh_token:
			persist_refresh_token(tokens.refresh_token, "", commit=True)
			self.settings.reload()
		return GraphClient(UrllibTransport(), tokens.access_token)

	def call(self, action: Callable[[GraphClient], _ActionResult]) -> _ActionResult:
		"""
		Run an action and refresh the token once if Graph returns 401.

		:param action: Callable that receives a Graph client.

		:returns: Whatever the action returns.
		"""
		try:
			return action(self.client)
		except GraphAuthError:
			self.client = self.connect()
			return action(self.client)


@frappe.whitelist(methods=["POST"])
def authorize_onedrive() -> dict[str, str]:
	"""
	Start Microsoft sign-in for the backup account.

	:returns: Authorization URL to open in the browser.
	"""
	frappe.only_for("System Manager", message=True)
	settings = frappe.get_doc(DOCTYPE_NAME)
	if not settings.enable:
		frappe.throw(_("Enable OneDrive backup before authorizing."))
	credentials = _credentials_or_throw(settings)
	verifier, challenge = create_pkce_pair()
	state = new_oauth_state()
	frappe.cache.set_value(
		state_cache_key(state),
		{"verifier": verifier, "user": frappe.session.user},
		expires_in_sec=OAUTH_STATE_TTL_SECONDS,
	)
	url = build_authorize_url(
		tenant=credentials["tenant"],
		client_id=credentials["client_id"],
		redirect_uri=credentials["redirect_uri"],
		state=state,
		code_challenge=challenge,
		scopes=delegated_scopes(settings.backup_target),
	)
	return {"url": url}


@frappe.whitelist(methods=["GET"])
def microsoft_oauth_callback(
	code: str | None = None,
	state: str | None = None,
	error: str | None = None,
	error_description: str | None = None,
) -> None:
	"""
	Hold the authorization code server-side and send the browser back to the form.

	The code is exchanged by a POST from the desk so the token is saved on a
	normal write request. This callback does not write to the database.

	:param code: Authorization code from Microsoft.
	:param state: Single-use state created when sign-in started.
	:param error: Error code from Microsoft, when consent fails.
	:param error_description: Error detail from Microsoft.
	"""
	frappe.only_for("System Manager", message=True)
	if error:
		_oauth_page(_("Authorization was not completed."), _short_oauth_error(error, error_description))
		return
	payload = consume_oauth_state(state or "")
	if payload is None or not code:
		_oauth_page(_("Authorization expired."), _("Start authorization again from OneDrive Settings."))
		return
	finish = new_oauth_state()
	frappe.cache.set_value(
		finish_cache_key(finish),
		{"code": code, "verifier": payload["verifier"], "user": frappe.session.user},
		expires_in_sec=OAUTH_STATE_TTL_SECONDS,
	)
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = "/app/onedrive-settings?onedrive_auth=" + quote(finish, safe="")


@frappe.whitelist(methods=["POST"])
def finish_onedrive_auth(finish_token: str) -> str:
	"""
	Exchange the stored authorization code and save the refresh token.

	:param finish_token: Single-use token from the redirect back to the form.

	:returns: Authorized account name, or a short status.
	"""
	frappe.only_for("System Manager", message=True)
	payload = consume_finish_payload(finish_token)
	settings = frappe.get_doc(DOCTYPE_NAME)
	credentials = _credentials_or_throw(settings)
	try:
		tokens = MicrosoftOAuth(
			UrllibTransport(),
			tenant=credentials["tenant"],
			client_id=credentials["client_id"],
			client_secret=credentials["client_secret"],
			redirect_uri=credentials["redirect_uri"],
			scopes=delegated_scopes(settings.backup_target),
		).exchange_code(payload["code"], payload["verifier"])
		if not tokens.refresh_token:
			raise SettingsInputError("missing_offline_access")
		account = GraphClient(UrllibTransport(), tokens.access_token).get_account_name()
	except SettingsInputError as error:
		frappe.throw(describe_input_error(error))
	except GraphError as error:
		frappe.throw(_("Authorization failed: {0}").format(str(error)))
	persist_refresh_token(tokens.refresh_token, account, commit=False)
	return account or _("Authorized")


@frappe.whitelist(methods=["POST"])
def take_backup() -> None:
	"""
	Queue a backup upload to OneDrive or SharePoint.
	"""
	frappe.only_for("System Manager", message=True)
	settings = frappe.get_doc(DOCTYPE_NAME)
	if not settings.enable:
		frappe.throw(_("Enable OneDrive backup before taking a backup."))
	if not settings.get_password("refresh_token", raise_exception=False):
		frappe.throw(_("Authorize OneDrive access before taking a backup."))
	enqueue(UPLOAD_METHOD, queue="long", timeout=1500, enqueue_after_commit=True)
	frappe.msgprint(_("Queued for backup. It may take a few minutes to an hour."))


def daily_backup() -> None:
	"""
	Upload when the saved frequency is Daily.
	"""
	scheduled_backup("Daily")


def weekly_backup() -> None:
	"""
	Upload when the saved frequency is Weekly.
	"""
	scheduled_backup("Weekly")


def monthly_backup() -> None:
	"""
	Upload when the saved frequency is Monthly.
	"""
	scheduled_backup("Monthly")


def scheduled_backup(frequency: str) -> None:
	"""
	Run a backup when this single is enabled for the given frequency.

	:param frequency: Daily, Weekly, or Monthly.
	"""
	if not cint(frappe.db.get_single_value(DOCTYPE_NAME, "enable")):
		return
	if frappe.db.get_single_value(DOCTYPE_NAME, "frequency") != frequency:
		return
	upload_system_backup_to_onedrive()


def upload_system_backup_to_onedrive(retry_count: int = 0) -> str:
	"""
	Create a Frappe backup and upload it to the configured drive.

	:param retry_count: How many times this upload has already been retried after a timeout.

	:returns: Short status for the scheduler.
	"""
	try:
		return upload_enabled_backup()
	except JobTimeoutException:
		if retry_count < 2:
			enqueue(UPLOAD_METHOD, queue="long", timeout=1500, retry_count=retry_count + 1)
			return "retry"
		notify_failure(SettingsInputError("timeout"))
		return "timeout"
	except Exception as error:
		# Returning keeps the failure email in this transaction. Re-raising would roll it back.
		notify_failure(error)
		return "failed"


def upload_enabled_backup() -> str:
	"""
	Upload the latest or newly created backup when the single is enabled.

	:returns: ok when the upload finished, or disabled when the single is off.
	"""
	settings = frappe.get_doc(DOCTYPE_NAME)
	if not settings.enable:
		return "disabled"
	set_progress(1, _("Backing up the site."))
	paths = backup_file_paths(settings)
	stamp = backup_group_key(os.path.basename(paths[0]))
	if not stamp:
		raise SettingsInputError("bad_filename")
	set_progress(2, _("Uploading the backup."))
	session = GraphSession(settings)
	location = session.call(lambda client: resolve_location(client, settings))
	dated_folder = session.call(
		lambda client: client.ensure_folder(location.drive_id, [stamp], parent_id=location.folder_id)
	)
	for path in paths:
		upload_one(session, location.drive_id, dated_folder, path)
	if settings.limit_no_of_backups:
		try:
			session.call(
				lambda client: prune_old_backups(
					client, location.drive_id, location.folder_id, cint(settings.no_of_backups)
				)
			)
		except Exception:
			frappe.log_error(
				title="OneDrive retention failed",
				message=redact_secrets(frappe.get_traceback()),
			)
	frappe.db.set_single_value(DOCTYPE_NAME, "last_backup_on", now_datetime())
	# The files are already on Microsoft. Keep the timestamp if notification fails.
	frappe.db.commit()  # nosemgrep: keep a successful upload timestamp if notification fails
	try:
		send_email(True, SERVICE_NAME, DOCTYPE_NAME, "send_notification_to")
	except Exception:
		frappe.log_error(
			title="OneDrive backup notification failed",
			message=redact_secrets(frappe.get_traceback()),
		)
	set_progress(3, _("Upload finished."))
	return "ok"


def backup_file_paths(settings: OneDriveSettings) -> list[str]:
	"""
	Return the database, site config, and optional file archives to upload.

	:param settings: OneDrive Settings.

	:returns: Absolute paths inside the site backups directory.
	"""
	validate_file_size()
	include_files = bool(settings.file_backup)
	if frappe.flags.create_new_backup:
		backup = new_backup(ignore_files=not include_files)
		paths = [backup.backup_path_db, backup.backup_path_conf]
		if include_files:
			paths.extend([backup.backup_path_files, backup.backup_path_private_files])
	elif include_files:
		database, config, public, private = get_latest_backup_file(with_files=True)
		if not public or not private:
			generate_files_backup()
			database, config, public, private = get_latest_backup_file(with_files=True)
		paths = [database, config, public, private]
	else:
		database, config = get_latest_backup_file()
		paths = [database, config]
	if any(not path for path in paths):
		raise SettingsInputError("backup_missing")
	return [str(path) for path in paths]


def resolve_location(client: GraphClient, settings: OneDriveSettings) -> DriveLocation:
	"""
	Find or create the backup folder on OneDrive or SharePoint.

	:param client: Authorized Graph client.
	:param settings: OneDrive Settings.

	:returns: Drive and folder ids.
	"""
	site_id = ""
	if settings.backup_target == "SharePoint":
		site = parse_sharepoint_site_url(settings.sharepoint_site_url or "")
		site_id = client.resolve_site_id(site.hostname, site.server_relative_path)
		library_name = (settings.document_library or "").strip() or "Documents"
		library = select_document_library(client.list_drives(site_id), library_name)
		drive_id = library.get("id")
		if not isinstance(drive_id, str) or not drive_id:
			raise GraphError("Microsoft Graph did not return a document library id.")
	else:
		drive_id = client.get_my_drive_id()
	folder_id = client.ensure_folder(drive_id, sanitize_folder_path(settings.backup_folder_name))
	frappe.db.set_single_value(
		DOCTYPE_NAME,
		{"backup_folder_id": folder_id, "drive_id": drive_id, "site_id": site_id},
	)
	return DriveLocation(drive_id=drive_id, folder_id=folder_id, site_id=site_id)


def upload_one(session: GraphSession, drive_id: str, folder_id: str, path: str) -> None:
	"""
	Upload one backup file from the site backups directory.

	:param session: Authorized Graph session.
	:param drive_id: Destination drive.
	:param folder_id: Dated backup folder.
	:param path: Path returned by the Frappe backup generator.
	"""
	local_path = local_backup_file(path)
	filename = assert_backup_filename(os.path.basename(local_path))
	size = os.path.getsize(local_path)
	if size <= 0:
		raise SettingsInputError("empty_file")

	def send(client: GraphClient) -> None:
		"""
		Stream the confined backup file to Graph.

		:param client: Authorized Graph client.
		"""
		# local_path is confined to the site backups directory before this open.
		with open(local_path, "rb") as backup_file:  # nosemgrep: frappe-security-file-traversal
			client.upload_file(drive_id, folder_id, filename, backup_file, size)

	session.call(send)


def prune_old_backups(client: GraphClient, drive_id: str, folder_id: str, keep: int) -> None:
	"""
	Delete dated backup groups beyond the number the site chose to keep.

	:param client: Authorized Graph client.
	:param drive_id: Destination drive.
	:param folder_id: Configured backup folder, not the dated subfolder.
	:param keep: Newest groups to retain.
	"""
	children = client.list_children(drive_id, folder_id)
	for item_id in select_items_to_delete(children, keep):
		client.delete_item(drive_id, item_id)


def local_backup_file(path: str) -> str:
	"""
	Map a backup generator path to a real file inside the backups directory.

	:param path: Path or file name from the backup generator.

	:returns: Real path of the backup file.
	"""
	root = get_backups_path()
	return confine_to_backups(os.path.join(root, os.path.basename(path or "")), root)


def app_credentials(settings: OneDriveSettings) -> dict[str, str]:
	"""
	Collect the Azure app credentials from site config or the form.

	Site config wins when its keys are set, so a bench can keep the secret out of the form.
	A scheduled upload uses this without a browser redirect URI.

	:param settings: OneDrive Settings.

	:returns: Tenant, client id, and client secret.
	"""
	tenant = configured_text(settings, "tenant_id", "onedrive_tenant_id") or "common"
	client_id = configured_text(settings, "client_id", "onedrive_client_id")
	client_secret = configured_secret(settings)
	if not client_id or not client_secret:
		raise SettingsInputError("missing_credentials")
	return {
		"tenant": validate_tenant(tenant),
		"client_id": validate_client_id(client_id),
		"client_secret": validate_client_secret(client_secret),
	}


def oauth_credentials(settings: OneDriveSettings) -> dict[str, str]:
	"""
	Collect app credentials plus the redirect URI for browser sign-in.

	:param settings: OneDrive Settings.

	:returns: Tenant, client id, client secret, and redirect URI.
	"""
	credentials = app_credentials(settings)
	credentials["redirect_uri"] = get_redirect_uri()
	return credentials


def configured_text(settings: OneDriveSettings, fieldname: str, config_key: str) -> str:
	"""
	Return the site-config value when it is set, otherwise the form value.

	:param settings: OneDrive Settings.
	:param fieldname: Field on the document.
	:param config_key: Key in site_config.json.

	:returns: Trimmed value, or an empty string.
	"""
	configured = frappe.conf.get(config_key)
	if _configured(configured):
		return str(configured).strip()
	value = settings.get(fieldname) or ""
	return value.strip() if isinstance(value, str) else ""


def configured_secret(settings: OneDriveSettings) -> str:
	"""
	Return the client secret from site config or the password field.

	:param settings: OneDrive Settings.

	:returns: Client secret, or an empty string.
	"""
	configured = frappe.conf.get("onedrive_client_secret")
	if _configured(configured):
		return str(configured).strip()
	return settings.get_password("client_secret", raise_exception=False) or ""


def get_redirect_uri() -> str:
	"""
	Return the redirect URI Microsoft must send the browser back to.

	:returns: Absolute callback URL.
	"""
	base = get_request_site_address(True).rstrip("/")
	return validate_redirect_uri(f"{base}{CALLBACK_PATH}")


def persist_refresh_token(refresh_token: str, authorized_user: str, *, commit: bool) -> None:
	"""
	Store a refresh token in the password field.

	:param refresh_token: Microsoft refresh token.
	:param authorized_user: Account name to display. Empty leaves the saved name unchanged.
	:param commit: Persist immediately when a later error must not roll the token back.
	"""
	# Password values live in __Auth. A placeholder in the Single tells the form it is set.
	set_encrypted_password(DOCTYPE_NAME, DOCTYPE_NAME, refresh_token, "refresh_token")
	values: dict[str, str] = {"refresh_token": PASSWORD_PLACEHOLDER}
	if authorized_user:
		values["authorized_user"] = " ".join(authorized_user.split())[:320]
	frappe.db.set_single_value(DOCTYPE_NAME, values)
	frappe.clear_document_cache(DOCTYPE_NAME, DOCTYPE_NAME)
	if commit:
		# Microsoft rotates refresh tokens. A later upload error must not restore the old token.
		frappe.db.commit()  # nosemgrep: keep a rotated refresh token if the rest of the upload fails


def consume_oauth_state(state: str) -> dict[str, str] | None:
	"""
	Read and delete the sign-in state for the current user.

	:param state: State from the Microsoft redirect.

	:returns: Verifier payload, or None when the state is missing or belongs to someone else.
	"""
	if not is_oauth_state(state):
		return None
	key = state_cache_key(state)
	payload = frappe.cache.get_value(key)
	frappe.cache.delete_value(key)
	if not isinstance(payload, dict) or payload.get("user") != frappe.session.user:
		return None
	verifier = payload.get("verifier")
	if not isinstance(verifier, str) or not verifier:
		return None
	return {"verifier": verifier}


def consume_finish_payload(finish_token: str) -> dict[str, str]:
	"""
	Read and delete the authorization code stored for the desk POST.

	:param finish_token: Token from the form redirect.

	:returns: Authorization code and PKCE verifier.
	"""
	if not is_oauth_state(finish_token):
		frappe.throw(_("Authorization expired. Start it again."))
	key = finish_cache_key(finish_token)
	payload = frappe.cache.get_value(key)
	frappe.cache.delete_value(key)
	code = payload.get("code") if isinstance(payload, dict) else None
	verifier = payload.get("verifier") if isinstance(payload, dict) else None
	user = payload.get("user") if isinstance(payload, dict) else None
	if user != frappe.session.user or not isinstance(code, str) or not isinstance(verifier, str):
		frappe.throw(_("Authorization expired. Start it again."))
	return {"code": code, "verifier": verifier}


def describe_input_error(error: SettingsInputError) -> str:
	"""
	Translate a settings error code for the desk.

	:param error: Safe error raised while checking settings or files.

	:returns: Message safe to show to a System Manager.
	"""
	messages = {
		"invalid_tenant": _("Directory (tenant) ID is not valid."),
		"invalid_client_id": _("Application (client) ID must be the GUID from the Azure app."),
		"invalid_client_secret": _("Client secret is empty or contains a line break."),
		"missing_credentials": _("Add the Azure client ID and client secret, or set them in site config."),
		"invalid_target": _("Choose OneDrive or SharePoint."),
		"folder_required": _("Enter a backup folder name."),
		"folder_traversal": _("Backup folder path cannot contain '..'."),
		"folder_character": _("Backup folder name contains a character OneDrive does not allow."),
		"folder_length": _("Backup folder path is too long."),
		"sharepoint_https": _("SharePoint site URL must start with https://."),
		"sharepoint_host": _("SharePoint site URL must be a SharePoint host."),
		"sharepoint_personal": _(
			"That host is a personal OneDrive. Choose the OneDrive target, or paste a SharePoint site URL."
		),
		"sharepoint_path": _("SharePoint site URL path is not valid."),
		"sharepoint_port": _("SharePoint site URL must use the default HTTPS port."),
		"invalid_url": _(
			"Set host_name in site config to the public https site URL before authorizing OneDrive."
		),
		"file_escape": _("Refusing to read a file outside the backups directory."),
		"file_missing": _("A backup file was not found in the site backups directory."),
		"empty_file": _("A backup file is empty."),
		"bad_filename": _("A backup file name cannot be uploaded."),
		"file_too_large": _("A backup file is larger than OneDrive can accept."),
		"invalid_chunk": _("Upload chunk size is not valid."),
		"library_missing": _("No document library matches that name."),
		"library_ambiguous": _("More than one document library has that name."),
		"missing_refresh_token": _("Authorize OneDrive access before taking a backup."),
		"missing_offline_access": _(
			"Microsoft did not grant offline access. Consent to offline_access and try again."
		),
		"backup_missing": _("A backup file is missing. Run the backup again."),
		"timeout": _("The backup upload timed out."),
		"retention_count": _("The number of backups to keep must be at least 1."),
	}
	if error.code == "library_missing" and error.detail:
		return _("No document library matches that name. Available libraries: {0}").format(error.detail)
	return messages.get(error.code, _("OneDrive settings are not valid."))


def notify_failure(error: BaseException) -> None:
	"""
	Record a failed backup and email the configured recipients.

	:param error: Exception that stopped the upload.
	"""
	try:
		frappe.log_error(title="OneDrive backup failed", message=redact_secrets(frappe.get_traceback()))
		send_email(False, SERVICE_NAME, DOCTYPE_NAME, "send_notification_to", failure_text(error))
	except Exception:
		frappe.log_error(
			title="OneDrive backup notification failed",
			message=redact_secrets(frappe.get_traceback()),
		)


def failure_text(error: BaseException) -> str:
	"""
	Return a short failure description that does not include tokens.

	:param error: Exception that stopped the upload.

	:returns: Text for the notification email.
	"""
	if isinstance(error, SettingsInputError):
		return describe_input_error(error)
	if isinstance(error, GraphError):
		return str(error)
	return _("The backup failed before the upload finished. See the Error Log for the details.")


def redact_secrets(text: str) -> str:
	"""
	Remove URLs and token parameters from text that may be logged.

	:param text: Traceback or error text.

	:returns: Redacted text.
	"""
	hidden = _SECRET_QUERY.sub(r"\1=[redacted]", text or "")
	return _URL_TEXT.sub("[url]", hidden)


def set_progress(progress: int, message: str) -> None:
	"""
	Publish upload progress to the user who started it.

	:param progress: Completed step.
	:param message: Short status.
	"""
	frappe.publish_realtime(
		"upload_to_onedrive",
		{"progress": progress, "total": 3, "message": message},
		user=frappe.session.user,
	)


def _credentials_or_throw(settings: OneDriveSettings) -> dict[str, str]:
	"""
	Return OAuth credentials or throw a translated validation error.

	:param settings: OneDrive Settings.

	:returns: Credential dictionary.
	"""
	try:
		return oauth_credentials(settings)
	except SettingsInputError as error:
		frappe.throw(describe_input_error(error))


def _validate_enabled(settings: OneDriveSettings) -> None:
	"""
	Check the fields required to schedule a backup.

	:param settings: OneDrive Settings being saved.
	"""
	if settings.backup_target not in {"OneDrive", "SharePoint"}:
		raise SettingsInputError("invalid_target")
	sanitize_folder_path(settings.backup_folder_name)
	if settings.backup_target == "SharePoint":
		parse_sharepoint_site_url(settings.sharepoint_site_url or "")
	if settings.frequency not in {"Daily", "Weekly", "Monthly"}:
		frappe.throw(_("Choose a backup frequency."))
	if settings.limit_no_of_backups and cint(settings.no_of_backups) < 1:
		raise SettingsInputError("retention_count")
	recipients = split_emails(settings.send_notification_to or "")
	if not recipients:
		frappe.throw(_("Add an email address for backup notifications."))
	for email in recipients:
		validate_email_address(email, throw=True)
	oauth_credentials(settings)


def _destination_changed(previous: OneDriveSettings, current: OneDriveSettings) -> bool:
	"""
	Return whether the saved destination no longer matches the form.

	:param previous: Document before this save.
	:param current: Document being saved.

	:returns: True when cached Graph ids must be cleared.
	"""
	return any(
		[
			previous.backup_target != current.backup_target,
			previous.backup_folder_name != current.backup_folder_name,
			(previous.sharepoint_site_url or "") != (current.sharepoint_site_url or ""),
			(previous.document_library or "") != (current.document_library or ""),
		]
	)


def _app_changed(previous: OneDriveSettings, current: OneDriveSettings) -> bool:
	"""
	Return whether the Azure app or the scopes have changed.

	:param previous: Document before this save.
	:param current: Document being saved.

	:returns: True when the refresh token must be cleared.
	"""
	return any(
		[
			previous.client_id != current.client_id,
			previous.tenant_id != current.tenant_id,
			previous.backup_target != current.backup_target,
		]
	)


def _configured(value: object) -> bool:
	"""
	Return whether a site-config value is a non-empty string.

	:param value: Value read from frappe.conf.

	:returns: True when the value can be used.
	"""
	return isinstance(value, str) and bool(value.strip())


def _oauth_page(title: str, message: str) -> None:
	"""
	Show an error page for a Microsoft redirect that did not finish.

	:param title: Page title.
	:param message: Page message.
	"""
	frappe.respond_as_web_page(title, message, indicator_color="red", http_status_code=417)


def _short_oauth_error(error: str, description: str | None) -> str:
	"""
	Keep Microsoft's error code and a short description.

	:param error: OAuth error code.
	:param description: OAuth error description.

	:returns: Single-line text.
	"""
	code = " ".join((error or "").split())[:80]
	detail = " ".join((description or "").split())[:240]
	if detail:
		return f"{code}: {detail}"
	return code or _("Authorization was not completed.")
