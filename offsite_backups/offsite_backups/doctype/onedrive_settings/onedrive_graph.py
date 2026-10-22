# OneDrive Graph Client - Revision 1 - 2026-10-22 09:10:00
# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE
"""Microsoft Graph and OAuth helpers for OneDrive and SharePoint backups.

This module does not import Frappe. The DocType controller supplies credentials
and the backup files. Network calls go through an HTTP transport so tests can
run without Microsoft.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import BinaryIO, Protocol
from urllib.parse import quote, unquote, urlencode, urlsplit

GRAPH_HOST = "graph.microsoft.com"
LOGIN_HOST = "login.microsoftonline.com"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
UPLOAD_ALIGNMENT = 320 * 1024
CHUNK_SIZE = 10 * 1024 * 1024
MAX_UPLOAD_BYTES = 250 * 1024 * 1024 * 1024
JSON_LIMIT = 2_000_000
STATE_CACHE_PREFIX = "offsite_backups_onedrive_oauth"
FINISH_CACHE_PREFIX = "offsite_backups_onedrive_finish"
CALLBACK_PATH = (
	"/api/method/offsite_backups.offsite_backups.doctype.onedrive_settings."
	"onedrive_settings.microsoft_oauth_callback"
)
UPLOAD_HOST_SUFFIXES = (
	".sharepoint.com",
	".sharepoint.us",
	".sharepoint.cn",
	".sharepoint.de",
	".1drv.com",
)
SHAREPOINT_SUFFIXES = (
	".sharepoint.com",
	".sharepoint.us",
	".sharepoint.cn",
	".sharepoint.de",
)
ONEDRIVE_SCOPES = "offline_access User.Read Files.ReadWrite"
SHAREPOINT_SCOPES = "offline_access User.Read Files.ReadWrite.All Sites.Read.All"
BACKUP_NAME_MARKERS = ("-database", "-site_config_backup", "-private-files", "-files")
RESERVED_FOLDER_NAMES = {
	"CON",
	"PRN",
	"AUX",
	"NUL",
	"COM1",
	"COM2",
	"COM3",
	"COM4",
	"COM5",
	"COM6",
	"COM7",
	"COM8",
	"COM9",
	"LPT1",
	"LPT2",
	"LPT3",
	"LPT4",
	"LPT5",
	"LPT6",
	"LPT7",
	"LPT8",
	"LPT9",
}
_TENANT_RE = re.compile(
	r"^(?:common|organizations|consumers|"
	r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|"
	r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
	r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+)$"
)
_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{20,200}$")
_STAMP_RE = re.compile(r"^\d{8}_\d{6}$")
_INVALID_NAME_CHARS = re.compile(r'[\x00-\x1f"*:<>?/\\|#%{}~]')


class SettingsInputError(ValueError):
	"""
	Raised when a backup setting or local file is not safe to use.
	"""

	def __init__(self, code: str, detail: str = "") -> None:
		"""
		Store a stable error code and an optional non-secret detail.

		:param code: Machine-readable reason.
		:param detail: Short extra context, such as library names.
		"""
		super().__init__(code)
		self.code = code
		self.detail = " ".join(detail.split())[:200]


class GraphError(Exception):
	"""
	Raised when Microsoft Graph or the token endpoint rejects a call.
	"""


class GraphAuthError(GraphError):
	"""
	Raised when Graph no longer accepts the access token.
	"""


class GraphNotFoundError(GraphError):
	"""
	Raised when a drive item or site does not exist.
	"""


class GraphConflictError(GraphError):
	"""
	Raised when a drive item already exists.
	"""


@dataclass(frozen=True)
class HttpRequest:
	"""
	One outbound HTTP request.
	"""

	method: str
	url: str
	headers: dict[str, str]
	body: bytes | None


@dataclass(frozen=True)
class HttpResponse:
	"""
	The status, headers, and body returned for one request.
	"""

	status: int
	headers: dict[str, str]
	body: bytes


@dataclass(frozen=True)
class TokenSet:
	"""
	Access token and the refresh token Microsoft returned with it.
	"""

	access_token: str
	refresh_token: str | None
	expires_in: int


@dataclass(frozen=True)
class SharePointSite:
	"""
	Host and server-relative path of a SharePoint site.
	"""

	hostname: str
	server_relative_path: str


@dataclass(frozen=True)
class RemoteChild:
	"""
	A file or folder directly inside the backup folder.
	"""

	item_id: str
	name: str
	is_folder: bool


class HttpTransport(Protocol):
	"""
	Send one HTTP request and return the raw response.
	"""

	def send(self, request: HttpRequest, timeout: float) -> HttpResponse:
		"""
		Perform the HTTP request.

		:param request: Method, URL, headers, and body.
		:param timeout: Socket timeout in seconds.

		:returns: Status, headers, and body.
		"""


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
	"""
	Refuse redirects so tokens cannot be replayed to another host.
	"""

	def redirect_request(self, req, fp, code, msg, headers, newurl):
		"""
		Stop the request when Microsoft returns a redirect.

		:param req: The original urllib request.
		:param fp: The response body.
		:param code: HTTP status.
		:param msg: HTTP reason.
		:param headers: Response headers.
		:param newurl: Redirect target.

		:returns: Nothing. A redirect is always rejected.
		"""
		del req, fp, code, msg, headers, newurl
		raise GraphError("Microsoft returned an unexpected redirect.")


class UrllibTransport:
	"""
	HTTPS client for Microsoft endpoints. Redirects are not followed.
	"""

	def send(self, request: HttpRequest, timeout: float) -> HttpResponse:
		"""
		Send one request with certificate verification.

		:param request: Method, URL, headers, and body.
		:param timeout: Socket timeout in seconds.

		:returns: Status, headers, and a size-limited body.
		"""
		assert_microsoft_request(request)
		outbound = urllib.request.Request(
			request.url,
			data=request.body,
			headers={key: value for key, value in request.headers.items()},
			method=request.method,
		)
		opener = urllib.request.build_opener(_RejectRedirect)
		try:
			with opener.open(outbound, timeout=timeout) as response:
				body = response.read(JSON_LIMIT + 1)
				status = int(getattr(response, "status", response.getcode()))
				headers = {key.lower(): value for key, value in response.headers.items()}
		except urllib.error.HTTPError as exc:
			try:
				body = exc.read(JSON_LIMIT + 1)
				status = int(exc.code)
				headers = {key.lower(): value for key, value in (exc.headers or {}).items()}
			finally:
				exc.close()
		except (urllib.error.URLError, TimeoutError, OSError):
			raise GraphError("Could not reach Microsoft.") from None
		if len(body) > JSON_LIMIT:
			raise GraphError("Microsoft returned a response that was too large.")
		return HttpResponse(status=status, headers=headers, body=body)


class MicrosoftOAuth:
	"""
	Exchange and refresh delegated Microsoft tokens.
	"""

	def __init__(
		self,
		transport: HttpTransport,
		*,
		tenant: str,
		client_id: str,
		client_secret: str,
		redirect_uri: str,
		scopes: str,
	) -> None:
		"""
		Store the app registration used for token calls.

		:param transport: HTTP client.
		:param tenant: Directory tenant, or common, organizations, or consumers.
		:param client_id: Azure application id.
		:param client_secret: Azure client secret.
		:param redirect_uri: Exact redirect URI registered on the Azure app.
		:param scopes: Space-separated delegated scopes.
		"""
		self.transport = transport
		self.tenant = validate_tenant(tenant)
		self.client_id = validate_client_id(client_id)
		self.client_secret = validate_client_secret(client_secret)
		self.redirect_uri = redirect_uri
		self.scopes = scopes

	def exchange_code(self, code: str, code_verifier: str) -> TokenSet:
		"""
		Turn an authorization code into tokens.

		:param code: Authorization code from the callback.
		:param code_verifier: PKCE verifier stored when sign-in started.

		:returns: Access token and refresh token.
		"""
		return self._token(
			{
				"client_id": self.client_id,
				"client_secret": self.client_secret,
				"grant_type": "authorization_code",
				"code": code,
				"redirect_uri": self.redirect_uri,
				"code_verifier": code_verifier,
				"scope": self.scopes,
			}
		)

	def refresh(self, refresh_token: str) -> TokenSet:
		"""
		Request a new access token.

		:param refresh_token: Refresh token from the last successful sign-in.

		:returns: Access token and, when Microsoft rotates it, a new refresh token.
		"""
		return self._token(
			{
				"client_id": self.client_id,
				"client_secret": self.client_secret,
				"grant_type": "refresh_token",
				"refresh_token": refresh_token,
				"scope": self.scopes,
			}
		)

	def _token(self, form: dict[str, str]) -> TokenSet:
		"""
		Post a form body to the tenant token endpoint.

		:param form: OAuth form fields.

		:returns: Parsed token response.
		"""
		url = f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token"
		request = HttpRequest(
			method="POST",
			url=url,
			headers={
				"Accept": "application/json",
				"Content-Type": "application/x-www-form-urlencoded",
			},
			body=urlencode(form).encode("utf-8"),
		)
		response = self.transport.send(request, timeout=60)
		payload = decode_json_object(response.body)
		if response.status != 200:
			raise GraphError(public_error_text(payload, response.status))
		return parse_token_set(payload)


class GraphClient:
	"""
	Call Microsoft Graph with one delegated access token.
	"""

	def __init__(
		self,
		transport: HttpTransport,
		access_token: str,
		*,
		sleeper: Callable[[float], None] = time.sleep,
		chunk_size: int = CHUNK_SIZE,
	) -> None:
		"""
		Store the token and upload chunk size.

		:param transport: HTTP client.
		:param access_token: Delegated Graph access token.
		:param sleeper: Wait function used between retries.
		:param chunk_size: Upload chunk size in bytes. Must be a multiple of 320 KiB.
		"""
		if not access_token or any(character.isspace() for character in access_token):
			raise GraphError("An access token is required.")
		if chunk_size <= 0 or chunk_size % UPLOAD_ALIGNMENT != 0:
			raise SettingsInputError("invalid_chunk")
		self.transport = transport
		self.access_token = access_token
		self.sleeper = sleeper
		self.chunk_size = chunk_size

	def get_account_name(self) -> str:
		"""
		Return the signed-in mailbox or user principal name.

		:returns: Account name, or an empty string when Graph omits it.
		"""
		payload = self.request("GET", "/me?$select=userPrincipalName,mail")
		mail = payload.get("mail")
		principal = payload.get("userPrincipalName")
		if isinstance(mail, str) and mail.strip():
			return " ".join(mail.split())
		if isinstance(principal, str):
			return " ".join(principal.split())
		return ""

	def get_my_drive_id(self) -> str:
		"""
		Return the signed-in user's OneDrive id.

		:returns: Drive id.
		"""
		payload = self.request("GET", "/me/drive?$select=id")
		return _required_text(payload.get("id"), "Microsoft Graph did not return a drive id.")

	def resolve_site_id(self, hostname: str, server_relative_path: str) -> str:
		"""
		Resolve a SharePoint site URL to a Graph site id.

		:param hostname: SharePoint hostname.
		:param server_relative_path: Site path, or empty for the root site.

		:returns: Graph site id.
		"""
		payload = self.request("GET", site_graph_path(hostname, server_relative_path))
		return _required_text(payload.get("id"), "Microsoft Graph did not return a SharePoint site id.")

	def list_drives(self, site_id: str) -> list[dict[str, object]]:
		"""
		List document libraries on a SharePoint site.

		:param site_id: Graph site id.

		:returns: Drive resources.
		"""
		path = f"/sites/{quote(site_id, safe='')}/drives?$select=id,name,driveType"
		return self.collect(path)

	def list_children(self, drive_id: str, item_id: str) -> list[RemoteChild]:
		"""
		List the files and folders inside one drive item.

		:param drive_id: Drive id.
		:param item_id: Parent folder id.

		:returns: Direct children.
		"""
		path = (
			f"/drives/{quote(drive_id, safe='')}/items/{quote(item_id, safe='')}/children"
			"?$select=id,name,folder,file&$top=200"
		)
		children: list[RemoteChild] = []
		for item in self.collect(path):
			child_id = item.get("id")
			name = item.get("name")
			if not isinstance(child_id, str) or not isinstance(name, str):
				continue
			children.append(
				RemoteChild(item_id=child_id, name=name, is_folder=isinstance(item.get("folder"), dict))
			)
		return children

	def ensure_folder(self, drive_id: str, parts: list[str], parent_id: str = "root") -> str:
		"""
		Create each missing folder in a path.

		:param drive_id: Drive id.
		:param parts: Folder names, from parent to child.
		:param parent_id: Existing parent item id. Use root for the drive root.

		:returns: Id of the deepest folder.
		"""
		current = parent_id
		for part in parts:
			current = self.ensure_child_folder(drive_id, current, part)
		return current

	def ensure_child_folder(self, drive_id: str, parent_id: str, name: str) -> str:
		"""
		Return the id of a child folder, creating it when it is missing.

		:param drive_id: Drive id.
		:param parent_id: Parent item id.
		:param name: Folder name.

		:returns: Child folder id.
		"""
		item_path = _child_path(drive_id, parent_id, name)
		try:
			item = self.request("GET", item_path)
		except GraphNotFoundError:
			try:
				item = self.request(
					"POST",
					f"/drives/{quote(drive_id, safe='')}/items/{quote(parent_id, safe='')}/children",
					json_body={
						"name": name,
						"folder": {},
						"@microsoft.graph.conflictBehavior": "fail",
					},
					expected=(200, 201),
				)
			except GraphConflictError:
				item = self.request("GET", item_path)
		if not isinstance(item.get("folder"), dict):
			raise GraphError("A file is using the backup folder name.")
		return _required_text(item.get("id"), "Microsoft Graph did not return a folder id.")

	def delete_item(self, drive_id: str, item_id: str) -> None:
		"""
		Delete a drive item. A missing item is already gone.

		:param drive_id: Drive id.
		:param item_id: Item id.
		"""
		try:
			self.request(
				"DELETE",
				f"/drives/{quote(drive_id, safe='')}/items/{quote(item_id, safe='')}",
				expected=(204,),
			)
		except GraphNotFoundError:
			return

	def upload_file(
		self,
		drive_id: str,
		parent_id: str,
		filename: str,
		stream: BinaryIO,
		file_size: int,
		progress: Callable[[int, int], None] | None = None,
	) -> str:
		"""
		Upload a backup with a Graph upload session.

		:param drive_id: Drive id.
		:param parent_id: Destination folder id.
		:param filename: Backup file name.
		:param stream: Binary stream positioned anywhere. The upload seeks within it.
		:param file_size: Exact size in bytes.
		:param progress: Optional callback receiving bytes sent and the total size.

		:returns: Uploaded item id when Graph returns one.
		"""
		safe_name = assert_backup_filename(filename)
		ranges = iter_upload_ranges(file_size, self.chunk_size)
		session = self.request(
			"POST",
			(
				f"/drives/{quote(drive_id, safe='')}/items/{quote(parent_id, safe='')}"
				f":/{quote(safe_name, safe='')}:/createUploadSession"
			),
			json_body={
				"item": {
					"@microsoft.graph.conflictBehavior": "replace",
					"name": safe_name,
				}
			},
			expected=(200, 201),
		)
		upload_url = session.get("uploadUrl")
		if not isinstance(upload_url, str):
			raise GraphError("Microsoft Graph did not return an upload session.")
		assert_upload_url(upload_url)
		item_id = ""
		for start, end in ranges:
			length = end - start + 1
			stream.seek(start)
			chunk = stream.read(length)
			if len(chunk) != length:
				raise GraphError("The backup file changed while it was uploading.")
			response = self.put_chunk(upload_url, chunk, start, end, file_size)
			uploaded_id = response.get("id")
			if isinstance(uploaded_id, str):
				item_id = uploaded_id
			if progress is not None:
				progress(end + 1, file_size)
		return item_id

	def put_chunk(self, upload_url: str, chunk: bytes, start: int, end: int, total: int) -> dict[str, object]:
		"""
		Upload one slice to a pre-authenticated session URL.

		:param upload_url: Session URL returned by Graph.
		:param chunk: Bytes for this slice.
		:param start: Inclusive start offset.
		:param end: Inclusive end offset.
		:param total: Full file size.

		:returns: Session progress or the final drive item.
		"""
		assert_upload_url(upload_url)
		request = HttpRequest(
			method="PUT",
			url=upload_url,
			headers={
				"Content-Length": str(len(chunk)),
				"Content-Range": content_range_header(start, end, total),
				"Content-Type": "application/octet-stream",
			},
			body=chunk,
		)
		return self.exchange(request, timeout=300, expected=(200, 201, 202))

	def collect(self, path: str) -> list[dict[str, object]]:
		"""
		Follow a Graph collection, including next links on graph.microsoft.com.

		:param path: Collection path or graph.microsoft.com URL.

		:returns: Items in the value arrays.
		"""
		items: list[dict[str, object]] = []
		next_path: str | None = path
		while next_path:
			payload = self.request("GET", next_path)
			value = payload.get("value")
			if not isinstance(value, list):
				raise GraphError("Microsoft Graph returned an unexpected list.")
			items.extend(item for item in value if isinstance(item, dict))
			if len(items) > 5000:
				raise GraphError("The backup folder has too many items to prune safely.")
			link = payload.get("@odata.nextLink")
			next_path = str(link) if isinstance(link, str) and link else None
		return items

	def request(
		self,
		method: str,
		path: str,
		*,
		json_body: dict[str, object] | None = None,
		expected: tuple[int, ...] = (200,),
	) -> dict[str, object]:
		"""
		Call Graph with the bearer token.

		:param method: HTTP method.
		:param path: Graph path, or an absolute graph.microsoft.com URL.
		:param json_body: Optional JSON body.
		:param expected: Success statuses.

		:returns: Parsed JSON object, or an empty object for 204.
		"""
		url = graph_url(path)
		headers = {
			"Authorization": f"Bearer {self.access_token}",
			"Accept": "application/json",
		}
		body = None
		if json_body is not None:
			body = json.dumps(json_body).encode("utf-8")
			headers["Content-Type"] = "application/json"
		return self.exchange(HttpRequest(method, url, headers, body), timeout=60, expected=expected)

	def exchange(
		self, request: HttpRequest, *, timeout: float, expected: tuple[int, ...]
	) -> dict[str, object]:
		"""
		Send a request and retry throttled or temporary failures.

		:param request: Prepared request.
		:param timeout: Socket timeout in seconds.
		:param expected: Success statuses.

		:returns: Parsed JSON object.
		"""
		delay = 1.0
		response: HttpResponse | None = None
		for attempt in range(5):
			response = self.transport.send(request, timeout)
			if response.status not in {429, 500, 502, 503, 504} or attempt == 4:
				break
			self.sleeper(retry_delay(response, delay))
			delay = min(delay * 2, 30)
		if response is None:
			raise GraphError("Microsoft did not return a response.")
		if response.status == 401 and _is_graph_host(request.url):
			raise GraphAuthError("Microsoft rejected the access token.")
		payload = {} if response.status == 204 or not response.body else decode_json_object(response.body)
		if response.status == 404:
			raise GraphNotFoundError(public_error_text(payload, response.status))
		if response.status == 409:
			raise GraphConflictError(public_error_text(payload, response.status))
		if response.status not in expected:
			raise GraphError(public_error_text(payload, response.status))
		return payload


def delegated_scopes(backup_target: str) -> str:
	"""
	Return the least Graph scopes that can write the selected destination.

	:param backup_target: OneDrive or SharePoint.

	:returns: Space-separated delegated scopes.
	"""
	if backup_target == "SharePoint":
		return SHAREPOINT_SCOPES
	if backup_target == "OneDrive":
		return ONEDRIVE_SCOPES
	raise SettingsInputError("invalid_target")


def validate_tenant(tenant: str) -> str:
	"""
	Accept a tenant id that cannot change the token host.

	:param tenant: Tenant value from the form or site config.

	:returns: Trimmed tenant.
	"""
	value = (tenant or "").strip()
	if not _TENANT_RE.match(value):
		raise SettingsInputError("invalid_tenant")
	return value


def validate_client_id(client_id: str) -> str:
	"""
	Accept an Azure application id.

	:param client_id: Application (client) ID.

	:returns: Canonical GUID.
	"""
	try:
		parsed = uuid.UUID((client_id or "").strip())
	except (ValueError, AttributeError):
		raise SettingsInputError("invalid_client_id") from None
	return str(parsed)


def validate_client_secret(secret: str) -> str:
	"""
	Reject an empty secret or one that contains a line break.

	:param secret: Azure client secret.

	:returns: The same secret when it is usable.
	"""
	if not secret or any(character in secret for character in "\r\n\x00") or len(secret) > 512:
		raise SettingsInputError("invalid_client_secret")
	return secret


def validate_redirect_uri(uri: str) -> str:
	"""
	Accept only this app's callback on https, or on local http for development.

	:param uri: Full redirect URI.

	:returns: The same URI when it is safe to register with Microsoft.
	"""
	parts = urlsplit(uri or "")
	host = (parts.hostname or "").lower()
	local = host == "localhost" or host.endswith(".localhost") or host == "127.0.0.1"
	if parts.username or parts.password or parts.query or parts.fragment or not host:
		raise SettingsInputError("invalid_url")
	if parts.path != CALLBACK_PATH:
		raise SettingsInputError("invalid_url")
	if parts.scheme == "https":
		return uri
	if parts.scheme == "http" and local:
		return uri
	raise SettingsInputError("invalid_url")


def create_pkce_pair() -> tuple[str, str]:
	"""
	Create a PKCE verifier and its S256 challenge.

	:returns: Verifier and challenge.
	"""
	verifier = secrets.token_urlsafe(64)
	digest = hashlib.sha256(verifier.encode("ascii")).digest()
	challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
	return verifier, challenge


def new_oauth_state() -> str:
	"""
	Create a single-use OAuth state token.

	:returns: URL-safe token.
	"""
	return secrets.token_urlsafe(32)


def is_oauth_state(value: str) -> bool:
	"""
	Return whether a value has the shape of an OAuth state token.

	:param value: Candidate token.

	:returns: True when the shape matches.
	"""
	return bool(_STATE_RE.match(value or ""))


def state_cache_key(state: str) -> str:
	"""
	Return the cache key for an in-progress sign-in.

	:param state: OAuth state.

	:returns: Cache key.
	"""
	return f"{STATE_CACHE_PREFIX}:{state}"


def finish_cache_key(token: str) -> str:
	"""
	Return the cache key that holds an authorization code until the desk saves it.

	:param token: Finish token.

	:returns: Cache key.
	"""
	return f"{FINISH_CACHE_PREFIX}:{token}"


def build_authorize_url(
	*,
	tenant: str,
	client_id: str,
	redirect_uri: str,
	state: str,
	code_challenge: str,
	scopes: str,
) -> str:
	"""
	Build the Microsoft authorize URL. The client secret is never included.

	:param tenant: Directory tenant.
	:param client_id: Azure application id.
	:param redirect_uri: Registered redirect URI.
	:param state: Single-use state.
	:param code_challenge: PKCE S256 challenge.
	:param scopes: Space-separated scopes.

	:returns: Authorize URL.
	"""
	safe_tenant = validate_tenant(tenant)
	query = urlencode(
		{
			"client_id": validate_client_id(client_id),
			"response_type": "code",
			"redirect_uri": redirect_uri,
			"response_mode": "query",
			"scope": scopes,
			"state": state,
			"code_challenge": code_challenge,
			"code_challenge_method": "S256",
			"prompt": "select_account",
		}
	)
	return f"https://login.microsoftonline.com/{safe_tenant}/oauth2/v2.0/authorize?{query}"


def parse_sharepoint_site_url(site_url: str) -> SharePointSite:
	"""
	Split a SharePoint site URL into the host and server-relative path.

	:param site_url: Absolute https URL of the site.

	:returns: Host and site path.
	"""
	parts = urlsplit((site_url or "").strip())
	if parts.scheme != "https":
		raise SettingsInputError("sharepoint_https")
	if parts.username or parts.password:
		raise SettingsInputError("sharepoint_path")
	if parts.port not in (None, 443):
		raise SettingsInputError("sharepoint_port")
	host = (parts.hostname or "").lower()
	if not host or not any(host.endswith(suffix) for suffix in SHAREPOINT_SUFFIXES):
		raise SettingsInputError("sharepoint_host")
	if "-my.sharepoint." in host:
		raise SettingsInputError("sharepoint_personal")
	path = unquote(parts.path or "")
	if "\\" in path or ".." in path.split("/"):
		raise SettingsInputError("sharepoint_path")
	return SharePointSite(hostname=host, server_relative_path=path.rstrip("/"))


def site_graph_path(hostname: str, server_relative_path: str) -> str:
	"""
	Return the Graph path that resolves a SharePoint site.

	:param hostname: SharePoint hostname.
	:param server_relative_path: Site path, or empty for the root site.

	:returns: Graph path beginning with /sites.
	"""
	host = hostname.lower()
	if not server_relative_path or server_relative_path == "/":
		return f"/sites/{host}"
	quoted = "/".join(quote(part, safe="") for part in server_relative_path.split("/"))
	return f"/sites/{host}:{quoted}"


def sanitize_folder_path(folder_path: str) -> list[str]:
	"""
	Split a backup folder path into safe OneDrive folder names.

	:param folder_path: Folder name or a slash-separated path.

	:returns: Folder names from parent to child.
	"""
	parts: list[str] = []
	for piece in (folder_path or "").strip().replace("\\", "/").split("/"):
		raw = piece.strip()
		if raw == "..":
			raise SettingsInputError("folder_traversal")
		name = raw.rstrip(". ")
		if not name or name == ".":
			continue
		if _INVALID_NAME_CHARS.search(name) or name.upper() in RESERVED_FOLDER_NAMES:
			raise SettingsInputError("folder_character")
		if len(name) > 200:
			raise SettingsInputError("folder_length")
		parts.append(name)
	if not parts:
		raise SettingsInputError("folder_required")
	if len(parts) > 20:
		raise SettingsInputError("folder_length")
	return parts


def sanitize_upload_name(filename: str) -> str:
	"""
	Return a single backup file name with no directory.

	:param filename: Original file name.

	:returns: Base name safe to append to a Graph path.
	"""
	name = os.path.basename(filename or "").strip()
	if not name or name in {".", ".."} or _INVALID_NAME_CHARS.search(name) or len(name) > 240:
		raise SettingsInputError("bad_filename")
	return name


def assert_backup_filename(filename: str) -> str:
	"""
	Accept only a Frappe backup archive name.

	:param filename: File name to upload.

	:returns: Safe file name.
	"""
	name = sanitize_upload_name(filename)
	if backup_group_key(name) is None or not any(marker in name for marker in BACKUP_NAME_MARKERS):
		raise SettingsInputError("bad_filename")
	return name


def backup_group_key(name: str) -> str | None:
	"""
	Return the Frappe backup timestamp prefix, when the name has one.

	:param name: File or folder name.

	:returns: YYYYMMDD_HHMMSS prefix, or None.
	"""
	stamp = (name or "").split("-", 1)[0]
	if _STAMP_RE.match(stamp):
		return stamp
	return None


def select_items_to_delete(children: list[RemoteChild], keep: int) -> list[str]:
	"""
	Choose old backup groups to delete. Unrelated files are kept.

	:param children: Direct children of the backup folder.
	:param keep: Number of newest timestamp groups to retain.

	:returns: Item ids to delete.
	"""
	if keep < 1:
		raise SettingsInputError("retention_count")
	grouped: dict[str, list[RemoteChild]] = {}
	for child in children:
		key = backup_group_key(child.name)
		if key is None:
			continue
		grouped.setdefault(key, []).append(child)
	expired = set(sorted(grouped, reverse=True)[keep:])
	return [child.item_id for key, members in grouped.items() if key in expired for child in members]


def iter_upload_ranges(file_size: int, chunk_size: int) -> list[tuple[int, int]]:
	"""
	Split a file into inclusive byte ranges. Only the last range may be short.

	:param file_size: File size in bytes.
	:param chunk_size: Chunk size in bytes.

	:returns: Inclusive start and end offsets.
	"""
	if chunk_size <= 0 or chunk_size % UPLOAD_ALIGNMENT != 0:
		raise SettingsInputError("invalid_chunk")
	if file_size <= 0:
		raise SettingsInputError("empty_file")
	if file_size > MAX_UPLOAD_BYTES:
		raise SettingsInputError("file_too_large")
	ranges: list[tuple[int, int]] = []
	start = 0
	while start < file_size:
		end = min(start + chunk_size, file_size) - 1
		ranges.append((start, end))
		start = end + 1
	return ranges


def content_range_header(start: int, end: int, total: int) -> str:
	"""
	Format a Graph Content-Range header.

	:param start: Inclusive start offset.
	:param end: Inclusive end offset.
	:param total: Full file size.

	:returns: Header value.
	"""
	return f"bytes {start}-{end}/{total}"


def confine_to_backups(candidate: str, backups_root: str) -> str:
	"""
	Resolve a path and require it to be a file inside the backups directory.

	:param candidate: Candidate file path.
	:param backups_root: Site backups directory.

	:returns: Real file path.
	"""
	root = os.path.realpath(backups_root)
	if not root or root == os.path.sep:
		raise SettingsInputError("file_escape")
	resolved = os.path.realpath(candidate)
	try:
		inside = os.path.commonpath([root, resolved]) == root
	except ValueError:
		inside = False
	if not inside:
		raise SettingsInputError("file_escape")
	if not os.path.isfile(resolved):
		raise SettingsInputError("file_missing")
	return resolved


def select_document_library(drives: list[dict[str, object]], library_name: str) -> dict[str, object]:
	"""
	Find one document library by name.

	:param drives: Drives returned by Graph.
	:param library_name: Library name, compared without case.

	:returns: The matching drive.
	"""
	wanted = (library_name or "").strip().casefold()
	matches = [drive for drive in drives if str(drive.get("name", "")).casefold() == wanted]
	if len(matches) == 1:
		return matches[0]
	if not matches:
		available = ", ".join(sorted(str(drive.get("name", "")) for drive in drives if drive.get("name")))
		raise SettingsInputError("library_missing", available)
	raise SettingsInputError("library_ambiguous")


def assert_upload_url(url: str) -> None:
	"""
	Allow a pre-authenticated upload URL only on a Microsoft file host.

	:param url: Upload session URL.
	"""
	if not _host_matches(url, UPLOAD_HOST_SUFFIXES):
		raise GraphError("Refusing an upload URL that is not a Microsoft host.")


def assert_microsoft_request(request: HttpRequest) -> None:
	"""
	Reject requests that are not https calls to Microsoft.

	:param request: Outbound request.
	"""
	parts = urlsplit(request.url)
	host = (parts.hostname or "").lower()
	if parts.scheme != "https" or not host or parts.username or parts.password:
		raise GraphError("Refusing a non-https Microsoft URL.")
	if host in {GRAPH_HOST, LOGIN_HOST}:
		return
	if any(host.endswith(suffix) for suffix in UPLOAD_HOST_SUFFIXES):
		return
	raise GraphError("Refusing an upload URL that is not a Microsoft host.")


def graph_url(path_or_url: str) -> str:
	"""
	Build a Graph URL and refuse any other host.

	:param path_or_url: Path beginning with /, or an absolute Graph URL.

	:returns: Absolute Graph URL.
	"""
	if path_or_url.startswith("https://"):
		if not _is_graph_host(path_or_url):
			raise GraphError("Refusing to send the access token to an unexpected host.")
		return path_or_url
	if not path_or_url.startswith("/"):
		raise GraphError("Graph paths must start with '/'.")
	return f"{GRAPH_BASE}{path_or_url}"


def parse_token_set(payload: dict[str, object]) -> TokenSet:
	"""
	Read tokens from a token-endpoint payload.

	:param payload: Parsed JSON object.

	:returns: Token set.
	"""
	access_token = payload.get("access_token")
	if not isinstance(access_token, str) or not access_token or any(char.isspace() for char in access_token):
		raise GraphError("Microsoft did not return an access token.")
	refresh_token = payload.get("refresh_token")
	if refresh_token is not None and (
		not isinstance(refresh_token, str) or any(char.isspace() for char in refresh_token)
	):
		raise GraphError("Microsoft returned an unusable refresh token.")
	token_type = payload.get("token_type")
	if isinstance(token_type, str) and token_type.lower() != "bearer":
		raise GraphError("Microsoft returned an unexpected token type.")
	return TokenSet(
		access_token=access_token,
		refresh_token=refresh_token if isinstance(refresh_token, str) else None,
		expires_in=_expires_in(payload.get("expires_in")),
	)


def decode_json_object(body: bytes) -> dict[str, object]:
	"""
	Parse a JSON object and reject anything else.

	:param body: Response body.

	:returns: JSON object.
	"""
	if not body:
		return {}
	try:
		parsed = json.loads(body.decode("utf-8"))
	except (UnicodeDecodeError, json.JSONDecodeError):
		raise GraphError("Microsoft returned a response that was not JSON.") from None
	if not isinstance(parsed, dict):
		raise GraphError("Microsoft returned an unexpected response.")
	return parsed


def public_error_text(payload: dict[str, object], status: int) -> str:
	"""
	Build a short error from Graph's code and message fields.

	:param payload: Parsed response.
	:param status: HTTP status.

	:returns: Text that does not include tokens or request URLs.
	"""
	error = payload.get("error")
	if isinstance(error, dict):
		code = _short_text(error.get("code")) or "Error"
		message = _short_text(error.get("message")) or "Request failed."
		return f"{code}: {message}"
	if isinstance(error, str):
		description = _short_text(payload.get("error_description")) or "Request failed."
		return f"{_short_text(error)}: {description}"
	return f"Microsoft Graph returned status {status}."


def retry_delay(response: HttpResponse, fallback: float) -> float:
	"""
	Return how long to wait after a throttled response.

	:param response: HTTP response.
	:param fallback: Delay used when Retry-After is missing.

	:returns: Delay in seconds, capped at 60.
	"""
	raw = response.headers.get("retry-after", "")
	if raw.isdigit():
		return min(float(raw), 60.0)
	return min(fallback, 60.0)


def _child_path(drive_id: str, parent_id: str, name: str) -> str:
	"""
	Return the Graph path of a named child.

	:param drive_id: Drive id.
	:param parent_id: Parent item id.
	:param name: Child name.

	:returns: Graph path.
	"""
	return f"/drives/{quote(drive_id, safe='')}/items/{quote(parent_id, safe='')}:/{quote(name, safe='')}"


def _required_text(value: object, message: str) -> str:
	"""
	Return a non-empty string or raise a Graph error.

	:param value: Candidate value.
	:param message: Safe error text.

	:returns: The string value.
	"""
	if not isinstance(value, str) or not value:
		raise GraphError(message)
	return value


def _expires_in(value: object) -> int:
	"""
	Normalize the token lifetime.

	:param value: expires_in value from Microsoft.

	:returns: Positive lifetime in seconds.
	"""
	if isinstance(value, bool) or not isinstance(value, int | str):
		return 3600
	try:
		parsed = int(value)
	except ValueError:
		return 3600
	if parsed <= 0:
		return 3600
	return parsed


def _short_text(value: object) -> str:
	"""
	Collapse whitespace and cap the length of external error text.

	:param value: Candidate text.

	:returns: Short text, or an empty string.
	"""
	if not isinstance(value, str):
		return ""
	return " ".join(value.split())[:240]


def _is_graph_host(url: str) -> bool:
	"""
	Return whether a URL is https on graph.microsoft.com.

	:param url: Absolute URL.

	:returns: True for the Graph host.
	"""
	parts = urlsplit(url)
	return parts.scheme == "https" and (parts.hostname or "").lower() == GRAPH_HOST and not parts.username


def _host_matches(url: str, suffixes: tuple[str, ...]) -> bool:
	"""
	Return whether an https URL host ends with one of the suffixes.

	:param url: Absolute URL.
	:param suffixes: Hostname suffixes, including the leading dot.

	:returns: True when the host is allowed.
	"""
	parts = urlsplit(url)
	host = (parts.hostname or "").lower()
	if parts.scheme != "https" or not host or parts.username or parts.password:
		return False
	return any(host.endswith(suffix) for suffix in suffixes)
