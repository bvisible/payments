from . import __version__ as app_version

app_name = "payments"
app_title = "Payments"
app_publisher = "Frappe Technologies"
app_description = "Payments app for frappe"
app_email = "hello@frappe.io"
app_license = "MIT"

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/pay/css/pay.css"
# app_include_js = "/assets/pay/js/pay.js"

# include js, css files in header of web template
# TWINT overlay (consumer-flow QR checkout) is loaded both on desk (for tests)
# and on public web pages (for the webshop checkout). Ships the `frappe.twint`
# namespace consumed by `webshop/templates/payments/twint.html`.
web_include_css = "/assets/payments/css/twint_dialog.css"
web_include_js = "/assets/payments/js/twint_dialog.js"
app_include_css = "/assets/payments/css/twint_dialog.css"
app_include_js = "/assets/payments/js/twint_dialog.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "pay/public/scss/website"

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

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "pay.utils.jinja_methods",
# 	"filters": "pay.utils.jinja_filters"
# }

# Installation
# ------------

before_install = "payments.utils.before_install"
after_install = "payments.utils.make_custom_fields"

# Uninstallation
# ------------

before_uninstall = "payments.utils.delete_custom_fields"
# after_uninstall = "pay.uninstall.after_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "pay.notifications.get_notification_config"

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

# DocType Class
# ---------------
# Override standard doctype classes

override_doctype_class = {"Web Form": "payments.overrides.payment_webform.PaymentWebForm"}

# Document Events
# ---------------
# Auto-reconcile a Payment Intent into its reference document (Sales Invoice / POS
# Invoice payments table) every time it is saved in the `succeeded` state.
# Idempotent thanks to a `reconciled_at` flag in `metadata_json`.

doc_events = {
	"Payment Intent": {
		"on_update": "payments.api.reconciliation.on_payment_intent_after_update",
	},
	"Sales Invoice": {
		# The card mentions a receipt must carry — masked PAN, scheme, AID,
		# authorisation. They live on the Payment Intent and nothing copied them
		# onto the invoice, so the till printed a sales receipt where a card receipt
		# was required. On validate rather than on_submit: a draft POS invoice is
		# printed too.
		"validate": "payments.api.card_receipt.fill_card_details",
	},
}

# Scheduled Tasks
# ---------------

scheduler_events = {
	"all": [
		"payments.payment_gateways.doctype.razorpay_settings.razorpay_settings.capture_payment",
	],
	"cron": {
		# Refresh online/offline status of registered Stripe Terminal readers.
		# Stripe flips Reader.status to offline after ~2 minutes without contact.
		"*/5 * * * *": [
			"payments.api.terminal.sync_stripe_readers_status",
		],
	},
}

# TWINT has no webhook stream, so we poll the bridge each minute for any
# Payment Intent in qr_bridge channel still in requires_action/processing.
scheduler_events["cron"]["* * * * *"] = [
	"payments.api.twint.poll_pending_twint_transactions",
]

# Wallee: webhook is primary, scheduler poll is the fallback. Runs every
# minute (not 5) — webshop wallee_web intents need the Sales Order finalized
# promptly when the buyer closed the /wallee/success tab before the Wallee
# sandbox settled the transaction. Covers both `terminal` and `wallee_web`.
# Idempotent.
scheduler_events["cron"]["* * * * *"].append(
	"payments.drivers.wallee.terminal_driver.poll_pending_transactions"
)

# Payrexx: the webhook is primary (one stream covers web, terminal and Tap to Pay).
# This poll only rescues web intents left non-final for 5+ minutes — a lost webhook,
# or a shopper who closed the tab before the return page ran. Kept every 5 minutes
# rather than every minute because Payrexx rate-limits at ~600 requests / 5 min per
# account, which a fleet of tills would otherwise burn through.
scheduler_events["cron"]["*/5 * * * *"].append(
	"payments.api.webhook_payrexx.poll_pending_payrexx_transactions"
)

# TWINT: daily certificate-expiry check → reminder email in the 45 days before
# the cert's notAfter (TWINT certs last ~3 years and expire silently).
scheduler_events["daily"] = [
	"payments.payment_gateways.doctype.twint_bridge_settings.twint_bridge_settings.check_certificate_expiry",
]

# Testing
# -------

before_tests = "erpnext.setup.utils.before_tests"  # To setup company and accounts

# Overriding Methods
# ------------------------------
#
override_whitelisted_methods = {
	"frappe.website.doctype.web_form.web_form.accept": "payments.overrides.payment_webform.accept"
}
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "pay.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]


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
# 	"pay.auth.validate"
# ]

# Translation
# --------------------------------

# Make link fields search translated document names for these DocTypes
# Recommended only for DocTypes which have limited documents with untranslated names
# For example: Role, Gender, etc.
# translated_search_doctypes = []
