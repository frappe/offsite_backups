// Copyright (c) 2017, Frappe Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("S3 Backup Settings", {
	refresh: function (frm) {
		frm.clear_custom_buttons();
		frm.events.add_test_connection_button(frm);
		frm.events.take_backup(frm);
		frm.events.update_field_hints(frm);
	},

	service_provider: function (frm) {
		frm.events.apply_provider_defaults(frm);
	},

	apply_provider_defaults: function (frm) {
		const provider = frm.doc.service_provider;
		if (provider === "Oracle Cloud Infrastructure (OCI)") {
			frm.set_value("addressing_style", "path");
			if (!frm.doc.endpoint_url || frm.doc.endpoint_url === "https://s3.amazonaws.com") {
				frm.set_value(
					"endpoint_url",
					"https://<namespace>.compat.objectstorage.<region>.oraclecloud.com"
				);
			}
		} else if (provider === "Cloudflare R2") {
			frm.set_value("addressing_style", "auto");
			if (!frm.doc.region_name) {
				frm.set_value("region_name", "auto");
			}
			if (!frm.doc.endpoint_url || frm.doc.endpoint_url === "https://s3.amazonaws.com") {
				frm.set_value("endpoint_url", "https://<account_id>.r2.cloudflarestorage.com");
			}
		} else if (provider === "MinIO") {
			frm.set_value("addressing_style", "path");
			if (!frm.doc.region_name) {
				frm.set_value("region_name", "us-east-1");
			}
			if (!frm.doc.endpoint_url || frm.doc.endpoint_url === "https://s3.amazonaws.com") {
				frm.set_value("endpoint_url", "http://localhost:9000");
			}
		} else if (provider === "Wasabi") {
			if (!frm.doc.endpoint_url || frm.doc.endpoint_url === "https://s3.amazonaws.com") {
				frm.set_value("endpoint_url", "https://s3.<region>.wasabisys.com");
			}
		} else if (provider === "Backblaze B2") {
			if (!frm.doc.endpoint_url || frm.doc.endpoint_url === "https://s3.amazonaws.com") {
				frm.set_value("endpoint_url", "https://s3.<region>.backblazeb2.com");
			}
		} else if (provider === "DigitalOcean Spaces") {
			frm.set_value("addressing_style", "path");
			if (!frm.doc.endpoint_url || frm.doc.endpoint_url === "https://s3.amazonaws.com") {
				frm.set_value("endpoint_url", "https://<region>.digitaloceanspaces.com");
			}
		} else if (provider === "Google Cloud Storage") {
			frm.set_value("addressing_style", "auto");
			if (!frm.doc.region_name) {
				frm.set_value("region_name", "auto");
			}
			if (!frm.doc.endpoint_url || frm.doc.endpoint_url === "https://s3.amazonaws.com") {
				frm.set_value("endpoint_url", "https://storage.googleapis.com");
			}
		} else if (provider === "Amazon S3") {
			frm.set_value("addressing_style", "auto");
			if (!frm.doc.endpoint_url) {
				frm.set_value("endpoint_url", "https://s3.amazonaws.com");
			}
		}
		frm.events.update_field_hints(frm);
	},

	update_field_hints: function (frm) {
		const provider = frm.doc.service_provider || "Amazon S3";
		if (provider === "Oracle Cloud Infrastructure (OCI)") {
			frm.set_df_property(
				"endpoint_url",
				"description",
				__(
					"OCI S3 Compatibility endpoint: <code>https://{namespace}.compat.objectstorage.{region}.oraclecloud.com</code> (replace {namespace} with your Tenancy Object Storage Namespace and {region} with your Region code, e.g. us-ashburn-1)"
				)
			);
			frm.set_df_property(
				"region_name",
				"description",
				__("Your OCI Region Identifier (e.g. <code>us-ashburn-1</code>, <code>ap-mumbai-1</code>, <code>eu-frankfurt-1</code>)")
			);
			frm.set_df_property(
				"access_key_id",
				"description",
				__("OCI Customer Secret Key (Access Key). Generated in OCI Console under User Settings &gt; Customer Secret Keys.")
			);
			frm.set_df_property(
				"secret_access_key",
				"description",
				__("OCI Customer Secret Key (Secret Key). Generated in OCI Console under User Settings &gt; Customer Secret Keys.")
			);
		} else {
			frm.set_df_property(
				"endpoint_url",
				"description",
				__("Endpoint URL for the storage service (e.g. https://s3.amazonaws.com for AWS S3).")
			);
			frm.set_df_property(
				"region_name",
				"description",
				__("Region identifier (e.g. us-east-1, ap-south-1, or auto).")
			);
			frm.set_df_property(
				"access_key_id",
				"description",
				__("Storage Access Key ID.")
			);
			frm.set_df_property(
				"secret_access_key",
				"description",
				__("Storage Secret Access Key.")
			);
		}
	},

	add_test_connection_button: function (frm) {
		frm.add_custom_button(__("Test Connection"), function () {
			if (!frm.doc.bucket || !frm.doc.access_key_id) {
				frappe.msgprint(__("Please fill in Bucket Name and API Access credentials first."));
				return;
			}
			frappe.call({
				method: "offsite_backups.offsite_backups.doctype.s3_backup_settings.s3_backup_settings.test_connection",
				freeze: true,
				freeze_message: __("Testing connection to object storage..."),
				callback: function (r) {
					if (!r.exc) {
						frappe.msgprint({
							title: __("Success"),
							indicator: "green",
							message: r.message ? r.message.message : __("Connection successful!"),
						});
					}
				},
			});
		});
	},

	take_backup: function (frm) {
		if (frm.doc.access_key_id && frm.doc.secret_access_key) {
			frm.add_custom_button(__("Take Backup Now"), function () {
				frm.dashboard.set_headline_alert(__("Backup Queued in Background..."));
				frappe.call({
					method: "offsite_backups.offsite_backups.doctype.s3_backup_settings.s3_backup_settings.take_backup",
					callback: function (r) {
						if (!r.exc) {
							frappe.show_alert({
								message: __("Backup job has been queued in background worker! Check email or Background Jobs for status."),
								indicator: "green",
							});
							frm.dashboard.clear_headline();
						}
					},
				});
			}).addClass("btn-primary");
		}
	},
});
