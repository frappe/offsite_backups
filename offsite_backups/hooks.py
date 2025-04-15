app_name = "offsite_backups"
app_title = "Offsite Backups"
app_publisher = "Frappe Technologies Pvt. Ltd."
app_description = "Various apps to backup frappe"
app_email = "developers@frappe.io"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "offsite_backups",
# 		"logo": "/assets/offsite_backups/logo.png",
# 		"title": "Offsite Backups",
# 		"route": "/offsite_backups",
# 		"has_permission": "offsite_backups.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/offsite_backups/css/offsite_backups.css"
# app_include_js = "/assets/offsite_backups/js/offsite_backups.js"

# include js, css files in header of web template
# web_include_css = "/assets/offsite_backups/css/offsite_backups.css"
# web_include_js = "/assets/offsite_backups/js/offsite_backups.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "offsite_backups/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "offsite_backups/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "offsite_backups.utils.jinja_methods",
# 	"filters": "offsite_backups.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "offsite_backups.install.before_install"
# after_install = "offsite_backups.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "offsite_backups.uninstall.before_uninstall"
# after_uninstall = "offsite_backups.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "offsite_backups.utils.before_app_install"
# after_app_install = "offsite_backups.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "offsite_backups.utils.before_app_uninstall"
# after_app_uninstall = "offsite_backups.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "offsite_backups.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

scheduler_events = {
# 	"all": [
# 		"offsite_backups.tasks.all"
# 	],
	"daily_long": [
		"offsite_backups.doctype.dropbox_settings.dropbox_settings.take_backups_daily",
		"offsite_backups.doctype.s3_backup_settings.s3_backup_settings.take_backups_daily",
  		"offsite_backups.doctype.google_drive.google_drive.daily_backup",
	],
	"weekly_long": [
	    "offsite_backups.doctype.dropbox_settings.dropbox_settings.take_backups_weekly",
		"offsite_backups.doctype.s3_backup_settings.s3_backup_settings.take_backups_weekly",
  		"offsite_backups.doctype.google_drive.google_drive.weekly_backup",
	],
    "monthly_long": [
		"offsite_backups.doctype.s3_backup_settings.s3_backup_settings.take_backups_monthly"
	],
}

# Testing
# -------

# before_tests = "offsite_backups.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "offsite_backups.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "offsite_backups.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["offsite_backups.utils.before_request"]
# after_request = ["offsite_backups.utils.after_request"]

# Job Events
# ----------
# before_job = ["offsite_backups.utils.before_job"]
# after_job = ["offsite_backups.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"offsite_backups.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

