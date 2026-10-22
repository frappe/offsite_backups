// OneDrive Settings - Revision 1 - 2026-10-22 09:10:00
// Copyright (c) 2026, Frappe Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("OneDrive Settings", {
	refresh(frm) {
		show_redirect_uri(frm);
		show_authorization_state(frm);
		bind_upload_progress(frm);
		show_backup_button(frm);
		frm.toggle_display(
			["client_id", "client_secret"],
			!frm.doc.__onload?.credentials_from_site_config
		);
		frm.toggle_display("tenant_id", !frm.doc.__onload?.tenant_from_site_config);
		finish_authorization(frm);
	},

	authorize_onedrive_access(frm) {
		if (!credentials_present(frm)) {
			frappe.msgprint(
				__("Add the Azure application ID and client secret before authorizing.")
			);
			return;
		}
		frappe.call({
			method: "offsite_backups.offsite_backups.doctype.onedrive_settings.onedrive_settings.authorize_onedrive",
			freeze: true,
			callback(response) {
				if (response.exc || !response.message?.url) {
					return;
				}
				const opened = window.open(response.message.url);
				if (!opened) {
					frappe.msgprint(
						__(
							"Allow pop-ups for this site, then click Authorize OneDrive Access again."
						)
					);
				}
			},
		});
	},
});

/**
 * Return whether the form or site config has an Azure client id and secret.
 *
 * @param frm OneDrive Settings form.
 *
 * @returns True when authorization can start.
 */
function credentials_present(frm) {
	return Boolean(
		frm.doc.__onload?.credentials_from_site_config ||
			(frm.doc.client_id && frm.doc.client_secret)
	);
}

/**
 * Show the redirect URI the Azure app must allow.
 *
 * @param frm OneDrive Settings form.
 */
function show_redirect_uri(frm) {
	if (frm.doc.__onload?.redirect_error) {
		frm.dashboard.set_headline(
			__("Set host_name in site config to the public https site URL before authorizing.")
		);
		return;
	}
	const redirect_uri = frm.doc.__onload?.redirect_uri;
	if (!redirect_uri) {
		return;
	}
	frm.set_df_property(
		"azure_setup_help",
		"options",
		`<p>${__(
			"Register this exact Web redirect URI on the Azure app."
		)}</p><p><code>${escape_html(redirect_uri)}</code></p>`
	);
}

/**
 * Show whether a refresh token is stored.
 *
 * @param frm OneDrive Settings form.
 */
function show_authorization_state(frm) {
	if (frm.doc.refresh_token) {
		frm.page.set_indicator(__("Authorized"), "green");
		return;
	}
	if (!frm.doc.enable) {
		return;
	}
	frm.page.set_indicator(__("Not Authorized"), "orange");
	if (!frm.doc.__onload?.redirect_error) {
		frm.dashboard.set_headline(
			__("Click Authorize OneDrive Access after the Azure app is registered.")
		);
	}
}

/**
 * Listen once for upload progress from the backup job.
 *
 * @param frm OneDrive Settings form.
 */
function bind_upload_progress(frm) {
	if (frm._onedrive_progress_bound) {
		return;
	}
	frm._onedrive_progress_bound = true;
	frappe.realtime.on("upload_to_onedrive", (data) => {
		if (!data?.progress) {
			return;
		}
		const title = __("Uploading to OneDrive");
		frm.dashboard.show_progress(title, (data.progress / data.total) * 100, data.message);
		if (data.progress === data.total) {
			frm.dashboard.hide_progress(title);
			frm.reload_doc();
		}
	});
}

/**
 * Add the manual backup button after the account is authorized.
 *
 * @param frm OneDrive Settings form.
 */
function show_backup_button(frm) {
	if (!(frm.doc.enable && frm.doc.refresh_token)) {
		return;
	}
	frm.add_custom_button(__("Take Backup"), () => {
		frappe.show_alert({
			indicator: "green",
			message: __("Backing up to OneDrive."),
		});
		frappe.call({
			method: "offsite_backups.offsite_backups.doctype.onedrive_settings.onedrive_settings.take_backup",
		});
	});
}

/**
 * Exchange the authorization code after Microsoft returns to the form.
 *
 * @param frm OneDrive Settings form.
 */
function finish_authorization(frm) {
	const params = new URLSearchParams(window.location.search);
	const finish_token = params.get("onedrive_auth");
	if (!finish_token || frm._onedrive_finish_started) {
		return;
	}
	frm._onedrive_finish_started = true;
	params.delete("onedrive_auth");
	const query = params.toString();
	window.history.replaceState({}, "", `${window.location.pathname}${query ? `?${query}` : ""}`);
	frappe.call({
		method: "offsite_backups.offsite_backups.doctype.onedrive_settings.onedrive_settings.finish_onedrive_auth",
		args: { finish_token },
		freeze: true,
		freeze_message: __("Saving OneDrive authorization"),
		callback(response) {
			if (!response.exc) {
				frm.reload_doc();
			}
		},
	});
}

/**
 * Escape text before it is inserted into the setup help HTML.
 *
 * @param value Redirect URI from the server.
 *
 * @returns Escaped text.
 */
function escape_html(value) {
	return String(value)
		.replaceAll("&", "&amp;")
		.replaceAll("<", "&lt;")
		.replaceAll(">", "&gt;")
		.replaceAll('"', "&quot;");
}
