# OneDrive Settings Tests - Revision 1 - 2026-10-22 09:10:00
# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE
"""Unit tests for the OneDrive and SharePoint backup client."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from base64 import urlsafe_b64encode
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

try:
	from frappe.tests import UnitTestCase
except ImportError:  # Running outside a bench, where Frappe is not installed.
	from unittest import TestCase as UnitTestCase

from offsite_backups.offsite_backups.doctype.onedrive_settings.onedrive_graph import (
	CALLBACK_PATH,
	CHUNK_SIZE,
	UPLOAD_ALIGNMENT,
	GraphAuthError,
	GraphClient,
	GraphError,
	HttpRequest,
	HttpResponse,
	MicrosoftOAuth,
	RemoteChild,
	SettingsInputError,
	_RejectRedirect,
	assert_backup_filename,
	assert_microsoft_request,
	assert_upload_url,
	backup_group_key,
	build_authorize_url,
	confine_to_backups,
	content_range_header,
	create_pkce_pair,
	delegated_scopes,
	finish_cache_key,
	iter_upload_ranges,
	parse_sharepoint_site_url,
	parse_token_set,
	public_error_text,
	sanitize_folder_path,
	select_document_library,
	select_items_to_delete,
	site_graph_path,
	state_cache_key,
	validate_redirect_uri,
	validate_tenant,
)

CLIENT_ID = "11111111-1111-1111-1111-111111111111"
REDIRECT_URI = "https://erp.example.com" + CALLBACK_PATH


class FakeTransport:
	"""
	HTTP transport that answers from a callback.
	"""

	def __init__(self, responder) -> None:
		"""
		Store the responder used for every request.

		:param responder: Callable that receives an HTTP request and returns a response.
		"""
		self.responder = responder
		self.requests: list[HttpRequest] = []

	def send(self, request: HttpRequest, timeout: float) -> HttpResponse:
		"""
		Record the request and return the scripted response.

		:param request: Outbound request.
		:param timeout: Unused timeout.

		:returns: Scripted response.
		"""
		del timeout
		self.requests.append(request)
		return self.responder(request)


def json_response(status: int, payload: dict, headers: dict[str, str] | None = None) -> HttpResponse:
	"""
	Build a JSON HTTP response.

	:param status: HTTP status.
	:param payload: JSON object.
	:param headers: Extra headers.

	:returns: HTTP response.
	"""
	response_headers = {"content-type": "application/json"}
	if headers:
		response_headers.update(headers)
	return HttpResponse(status, response_headers, json.dumps(payload).encode())


class UnitTestOneDriveSettings(UnitTestCase):
	"""
	Test Graph upload rules without a network or a Frappe site.
	"""

	def test_chunk_size_matches_graph_alignment(self) -> None:
		"""
		Require the production chunk size to be a multiple of 320 KiB.
		"""
		self.assertEqual(CHUNK_SIZE % UPLOAD_ALIGNMENT, 0)
		ranges = iter_upload_ranges(CHUNK_SIZE + 10, CHUNK_SIZE)
		self.assertEqual(ranges, [(0, CHUNK_SIZE - 1), (CHUNK_SIZE, CHUNK_SIZE + 9)])
		self.assertEqual((ranges[0][1] - ranges[0][0] + 1) % UPLOAD_ALIGNMENT, 0)

	def test_content_range_header(self) -> None:
		"""
		Format the inclusive range Microsoft expects.
		"""
		self.assertEqual(content_range_header(0, 9, 10), "bytes 0-9/10")

	def test_empty_and_invalid_ranges_are_rejected(self) -> None:
		"""
		Reject an empty file and a chunk size Graph would refuse.
		"""
		with self.assertRaises(SettingsInputError) as empty:
			iter_upload_ranges(0, UPLOAD_ALIGNMENT)
		self.assertEqual(empty.exception.code, "empty_file")
		with self.assertRaises(SettingsInputError) as chunk:
			iter_upload_ranges(10, UPLOAD_ALIGNMENT + 1)
		self.assertEqual(chunk.exception.code, "invalid_chunk")

	def test_pkce_challenge_matches_verifier(self) -> None:
		"""
		Build the S256 challenge Microsoft verifies.
		"""
		verifier, challenge = create_pkce_pair()
		digest = hashlib.sha256(verifier.encode("ascii")).digest()
		expected = urlsafe_b64encode(digest).decode("ascii").rstrip("=")
		self.assertEqual(challenge, expected)
		self.assertNotIn("=", challenge)
		self.assertNotIn(verifier, challenge)

	def test_authorize_url_does_not_carry_the_secret(self) -> None:
		"""
		Keep the client secret and PKCE verifier off the browser URL.
		"""
		verifier, challenge = create_pkce_pair()
		url = build_authorize_url(
			tenant="organizations",
			client_id=CLIENT_ID,
			redirect_uri=REDIRECT_URI,
			state="state-token-value-1234567890",
			code_challenge=challenge,
			scopes=delegated_scopes("OneDrive"),
		)
		parts = urlsplit(url)
		query = parse_qs(parts.query)
		self.assertEqual(parts.hostname, "login.microsoftonline.com")
		self.assertEqual(query["client_id"], [CLIENT_ID])
		self.assertEqual(query["code_challenge"], [challenge])
		self.assertEqual(query["code_challenge_method"], ["S256"])
		self.assertNotIn("client_secret", url)
		self.assertNotIn(verifier, url)
		self.assertNotIn("Sites.Read.All", query["scope"][0])

	def test_sharepoint_scopes_include_site_access(self) -> None:
		"""
		Request site access only for the SharePoint target.
		"""
		scopes = delegated_scopes("SharePoint")
		self.assertIn("Files.ReadWrite.All", scopes)
		self.assertIn("Sites.Read.All", scopes)
		self.assertIn("offline_access", scopes)

	def test_tenant_rejects_path_injection(self) -> None:
		"""
		Reject a tenant value that could change the token host.
		"""
		self.assertEqual(validate_tenant("common"), "common")
		self.assertEqual(validate_tenant(CLIENT_ID), CLIENT_ID)
		self.assertEqual(validate_tenant("contoso.onmicrosoft.com"), "contoso.onmicrosoft.com")
		for value in ("", "common/../../evil", "bad tenant", "https://evil.example"):
			with self.assertRaises(SettingsInputError) as caught:
				validate_tenant(value)
			self.assertEqual(caught.exception.code, "invalid_tenant")

	def test_redirect_uri_allows_only_this_callback(self) -> None:
		"""
		Allow the app callback on https, and on local http for development.
		"""
		self.assertEqual(validate_redirect_uri(REDIRECT_URI), REDIRECT_URI)
		local = "http://site.localhost" + CALLBACK_PATH
		self.assertEqual(validate_redirect_uri(local), local)
		for value in (
			"http://erp.example.com" + CALLBACK_PATH,
			"https://erp.example.com/app",
			"https://user@erp.example.com" + CALLBACK_PATH,
		):
			with self.assertRaises(SettingsInputError) as caught:
				validate_redirect_uri(value)
			self.assertEqual(caught.exception.code, "invalid_url")

	def test_sharepoint_site_url(self) -> None:
		"""
		Parse SharePoint site URLs and reject OneDrive personal hosts.
		"""
		site = parse_sharepoint_site_url("https://contoso.sharepoint.com/sites/Backups/")
		self.assertEqual(site.hostname, "contoso.sharepoint.com")
		self.assertEqual(site.server_relative_path, "/sites/Backups")
		self.assertEqual(
			site_graph_path(site.hostname, site.server_relative_path),
			"/sites/contoso.sharepoint.com:/sites/Backups",
		)
		root = parse_sharepoint_site_url("https://contoso.sharepoint.com")
		self.assertEqual(
			site_graph_path(root.hostname, root.server_relative_path), "/sites/contoso.sharepoint.com"
		)
		teams = parse_sharepoint_site_url("https://contoso.sharepoint.us/teams/Ops")
		self.assertEqual(teams.hostname, "contoso.sharepoint.us")
		cases = {
			"http://contoso.sharepoint.com/sites/Backups": "sharepoint_https",
			"https://contoso-my.sharepoint.com/personal/user": "sharepoint_personal",
			"https://evil.example/sites/Backups": "sharepoint_host",
			"https://user:pass@contoso.sharepoint.com/sites/Backups": "sharepoint_path",
			"https://contoso.sharepoint.com:8443/sites/Backups": "sharepoint_port",
			"https://contoso.sharepoint.com/sites/../Backups": "sharepoint_path",
		}
		for url, code in cases.items():
			with self.assertRaises(SettingsInputError) as caught:
				parse_sharepoint_site_url(url)
			self.assertEqual(caught.exception.code, code)

	def test_folder_path_is_sanitized(self) -> None:
		"""
		Turn a folder path into safe names and reject traversal.
		"""
		self.assertEqual(sanitize_folder_path(r"ERPNext\./Daily."), ["ERPNext", "Daily"])
		for value, code in (
			("", "folder_required"),
			("ERPNext/../../secret", "folder_traversal"),
			("ERPNext/CON", "folder_character"),
			("ERPNext/bad:name", "folder_character"),
		):
			with self.assertRaises(SettingsInputError) as caught:
				sanitize_folder_path(value)
			self.assertEqual(caught.exception.code, code)

	def test_backup_filenames(self) -> None:
		"""
		Accept Frappe backup archives and reject other names.
		"""
		for name in (
			"20261022_090000-site-database.sql.gz",
			"20261022_090000-site-partial-database.sql.gz",
			"20261022_090000-site-database-enc.sql.gz",
			"20261022_090000-site-site_config_backup.json",
			"20261022_090000-site-site_config_backup-enc.json",
			"20261022_090000-site-files.tar",
			"20261022_090000-site-files.tgz",
			"20261022_090000-site-files-enc.tar",
			"20261022_090000-site-private-files.tar",
			"20261022_090000-site-private-files-enc.tgz",
		):
			self.assertEqual(assert_backup_filename(name), name)
		self.assertEqual(backup_group_key("20261022_090000"), "20261022_090000")
		with self.assertRaises(SettingsInputError):
			assert_backup_filename("notes.txt")
		with self.assertRaises(SettingsInputError):
			assert_backup_filename("20261022_090000-site-database.sql.gz/../../x")

	def test_retention_keeps_newest_groups_and_unrelated_files(self) -> None:
		"""
		Delete expired backup groups without deleting unrelated files.
		"""
		children = [
			RemoteChild("old-folder", "20261020_090000", True),
			RemoteChild("mid-file", "20261021_090000-site-database.sql.gz", False),
			RemoteChild("new-folder", "20261022_090000", True),
			RemoteChild("notes", "readme.txt", False),
		]
		self.assertEqual(select_items_to_delete(children, 2), ["old-folder"])
		self.assertEqual(select_items_to_delete(children, 3), [])

	def test_document_library_match_is_case_insensitive(self) -> None:
		"""
		Select one library by name and report the available names when none match.
		"""
		drives = [{"id": "1", "name": "Documents"}, {"id": "2", "name": "Archive"}]
		self.assertEqual(select_document_library(drives, "documents")["id"], "1")
		with self.assertRaises(SettingsInputError) as missing:
			select_document_library(drives, "Missing")
		self.assertEqual(missing.exception.code, "library_missing")
		self.assertIn("Archive", missing.exception.detail)
		with self.assertRaises(SettingsInputError) as ambiguous:
			select_document_library(
				[{"id": "1", "name": "Documents"}, {"id": "2", "name": "documents"}], "Documents"
			)
		self.assertEqual(ambiguous.exception.code, "library_ambiguous")

	def test_confine_to_backups_rejects_symlinks(self) -> None:
		"""
		Read only a real file inside the backups directory.
		"""
		with tempfile.TemporaryDirectory() as root:
			backup = Path(root) / "20261022_090000-site-database.sql.gz"
			backup.write_text("backup", encoding="utf-8")
			self.assertEqual(confine_to_backups(str(backup), root), str(backup.resolve()))
			outside = Path(root).parent / "outside-onedrive-test.txt"
			outside.write_text("secret", encoding="utf-8")
			link = Path(root) / "link.txt"
			try:
				link.symlink_to(outside)
				with self.assertRaises(SettingsInputError) as caught:
					confine_to_backups(str(link), root)
				self.assertEqual(caught.exception.code, "file_escape")
			finally:
				outside.unlink(missing_ok=True)

	def test_cache_keys_are_separate(self) -> None:
		"""
		Keep the sign-in state and the finish token in different cache keys.
		"""
		self.assertNotEqual(state_cache_key("same-token"), finish_cache_key("same-token"))

	def test_token_parser_rejects_whitespace(self) -> None:
		"""
		Reject tokens that contain whitespace.
		"""
		parsed = parse_token_set(
			{
				"access_token": "access-token",
				"refresh_token": "refresh-token",
				"expires_in": "30",
				"token_type": "Bearer",
			}
		)
		self.assertEqual(parsed.access_token, "access-token")
		self.assertEqual(parsed.expires_in, 30)
		with self.assertRaises(GraphError):
			parse_token_set({"access_token": "bad token"})

	def test_refresh_posts_only_to_microsoft(self) -> None:
		"""
		Send the client secret to the token host and nowhere else.
		"""

		def responder(request: HttpRequest) -> HttpResponse:
			self.assertEqual(urlsplit(request.url).hostname, "login.microsoftonline.com")
			self.assertNotIn("Authorization", request.headers)
			return json_response(
				200,
				{"access_token": "access-token", "refresh_token": "rotated-token", "expires_in": 3600},
			)

		transport = FakeTransport(responder)
		tokens = MicrosoftOAuth(
			transport,
			tenant="organizations",
			client_id=CLIENT_ID,
			client_secret="unit-test-secret",
			redirect_uri=REDIRECT_URI,
			scopes=delegated_scopes("SharePoint"),
		).refresh("old-refresh-token")
		body = transport.requests[0].body.decode()
		self.assertIn("unit-test-secret", body)
		self.assertIn("old-refresh-token", body)
		self.assertIn("Sites.Read.All", body)
		self.assertEqual(tokens.refresh_token, "rotated-token")

	def test_upload_does_not_send_the_access_token_to_the_upload_host(self) -> None:
		"""
		Use the bearer token on Graph and the pre-authenticated URL for slices.
		"""
		payload = b"a" * 400_000
		puts: list[HttpRequest] = []

		def responder(request: HttpRequest) -> HttpResponse:
			if request.method == "POST":
				self.assertIn("graph.microsoft.com", request.url)
				self.assertEqual(request.headers.get("Authorization"), "Bearer access-token")
				self.assertIn("createUploadSession", request.url)
				return json_response(
					200, {"uploadUrl": "https://contoso.sharepoint.com/upload?tempauth=secret"}
				)
			puts.append(request)
			header = request.headers["Content-Range"]
			if header.startswith("bytes 327680-"):
				return json_response(200, {"id": "uploaded"})
			return json_response(202, {})

		client = GraphClient(
			FakeTransport(responder), "access-token", sleeper=lambda seconds: None, chunk_size=327680
		)
		item_id = client.upload_file(
			"drive",
			"parent",
			"20261022_090000-site-database.sql.gz",
			io.BytesIO(payload),
			len(payload),
		)
		self.assertEqual(item_id, "uploaded")
		self.assertEqual(len(puts), 2)
		self.assertNotIn("Authorization", puts[0].headers)
		self.assertNotIn("access-token", puts[0].url)
		self.assertEqual(puts[0].headers["Content-Range"], "bytes 0-327679/400000")
		self.assertEqual(puts[1].headers["Content-Range"], "bytes 327680-399999/400000")
		self.assertEqual(puts[0].headers["Content-Type"], "application/octet-stream")

	def test_graph_auth_failure_is_not_retried(self) -> None:
		"""
		Surface a rejected access token immediately.
		"""
		calls = {"count": 0}

		def responder(request: HttpRequest) -> HttpResponse:
			del request
			calls["count"] += 1
			return json_response(401, {"error": {"code": "InvalidAuthenticationToken", "message": "expired"}})

		client = GraphClient(FakeTransport(responder), "access-token", sleeper=lambda seconds: None)
		with self.assertRaises(GraphAuthError):
			client.get_my_drive_id()
		self.assertEqual(calls["count"], 1)

	def test_throttled_graph_call_retries(self) -> None:
		"""
		Retry a throttled Graph call and honor Retry-After.
		"""
		delays: list[float] = []
		attempts = {"count": 0}

		def responder(request: HttpRequest) -> HttpResponse:
			del request
			attempts["count"] += 1
			if attempts["count"] == 1:
				return json_response(
					429, {"error": {"code": "tooMany", "message": "slow down"}}, {"retry-after": "2"}
				)
			return json_response(200, {"id": "drive-id"})

		client = GraphClient(FakeTransport(responder), "access-token", sleeper=delays.append)
		self.assertEqual(client.get_my_drive_id(), "drive-id")
		self.assertEqual(delays, [2.0])

	def test_next_link_must_stay_on_graph(self) -> None:
		"""
		Refuse a collection link that would send the access token elsewhere.
		"""

		def responder(request: HttpRequest) -> HttpResponse:
			self.assertIn("graph.microsoft.com", request.url)
			return json_response(200, {"value": [], "@odata.nextLink": "https://evil.example/steal"})

		client = GraphClient(FakeTransport(responder), "access-token", sleeper=lambda seconds: None)
		with self.assertRaises(GraphError):
			client.list_children("drive", "folder")

	def test_upload_url_host_is_restricted(self) -> None:
		"""
		Reject upload URLs that are not Microsoft file hosts.
		"""
		assert_upload_url("https://contoso.sharepoint.com/personal/upload")
		assert_upload_url("https://sn3302.up.1drv.com/upload")
		for url in (
			"http://contoso.sharepoint.com/upload",
			"https://evil.example/sharepoint.com",
			"https://contoso.sharepoint.com.evil.example/upload",
			"https://user@contoso.sharepoint.com/upload",
		):
			with self.assertRaises(GraphError):
				assert_upload_url(url)

	def test_transport_rejects_non_https_before_connecting(self) -> None:
		"""
		Reject a clear-text Microsoft URL without opening a socket.
		"""
		request = HttpRequest("GET", "http://graph.microsoft.com/v1.0/me", {}, None)
		with self.assertRaises(GraphError):
			assert_microsoft_request(request)

	def test_redirects_are_rejected(self) -> None:
		"""
		Stop a redirect before the client follows it.
		"""
		with self.assertRaises(GraphError):
			_RejectRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example")

	def test_folder_creation_uses_a_folder_facet(self) -> None:
		"""
		Create a missing folder and then reuse it.
		"""
		seen: list[str] = []

		def responder(request: HttpRequest) -> HttpResponse:
			seen.append(request.method)
			if request.method == "GET":
				return json_response(404, {"error": {"code": "itemNotFound", "message": "missing"}})
			body = json.loads(request.body.decode())
			self.assertEqual(body["folder"], {})
			self.assertEqual(body["@microsoft.graph.conflictBehavior"], "fail")
			self.assertEqual(request.headers["Authorization"], "Bearer access-token")
			return json_response(201, {"id": "folder-id", "folder": {}})

		client = GraphClient(FakeTransport(responder), "access-token", sleeper=lambda seconds: None)
		self.assertEqual(client.ensure_folder("drive", ["ERPNext"]), "folder-id")
		self.assertEqual(seen, ["GET", "POST"])

	def test_public_error_omits_the_raw_body(self) -> None:
		"""
		Show Graph's error code without echoing an unexpected body.
		"""
		response = HttpResponse(500, {}, b"access_token=secret https://contoso.sharepoint.com/tempauth=abc")
		text = public_error_text({}, response.status)
		self.assertNotIn("secret", text)
		self.assertNotIn("http", text)
		self.assertIn("500", text)


if __name__ == "__main__":
	unittest.main()
