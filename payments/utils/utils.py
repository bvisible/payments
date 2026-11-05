import click
import frappe
from frappe import _
from contextlib import contextmanager
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

# //// Neoffice — added imports: `require_text`, `safe_redirect_to` and the limit on `get_checkout_url`.
from frappe.rate_limiter import rate_limit
from frappe.utils.data import get_host_name_from_request, get_url
from urllib.parse import urlsplit


# //// Neoffice — added helper (no upstream equivalent). A page takes no annotation and `frappe.form_dict`
# //// keeps the list or the dict of a JSON body; `frappe.get_doc` and `frappe.db.*` then read it as a filter
# //// of the caller's choice, operators included, and a guest could test any stored value one bit at a time.
# //// A request names a document by a text, or not at all.
def require_text(*values) -> None:
	"""Refuse (403) a request value that is neither absent nor a text."""
	for value in values:
		if value is not None and not isinstance(value, str):
			frappe.throw(_("Not permitted"), frappe.PermissionError)


# //// Neoffice — added helper (no upstream equivalent). The result pages followed `redirect_to` as it came,
# //// so a link on the shop's own address could end on any other site or run a `javascript:` URL. Core's
# //// `frappe.www.login.sanitize_redirect` is the login page's: it rewrites a refused target to /app and lets
# //// `javascript:` through, so the rule is spelled out here.
def safe_redirect_to(target) -> str | None:
	"""Return ``target`` when a browser sent there stays on this site, else None.

	A path stays, and so does an http(s) address on the host of this request or the site's own. What a
	browser reads differently from ``urlsplit`` is refused first: it turns a backslash into a slash, drops
	tabs and newlines, and takes any number of leading slashes for the start of a host.
	"""
	if not target or not isinstance(target, str):
		return None
	target = target.strip()
	if "\\" in target or any(char < " " for char in target):
		return None
	try:
		parts = urlsplit(target)
	except ValueError:
		return None
	if not parts.scheme and not parts.netloc:
		return None if target.startswith("//") else target
	if parts.scheme not in ("", "http", "https"):
		return None
	site_hosts = {
		urlsplit(get_url()).netloc.lower(),
		urlsplit(get_host_name_from_request() or "").netloc.lower(),
	}
	return target if parts.netloc and parts.netloc.lower() in site_hosts else None


def validate_integration_request(docname: str | None):
	# //// Neoffice — the Razorpay and Paytm checkout pages pass the link's own token here: see `require_text`.
	require_text(docname)
	if frappe.db.get_value("Integration Request", docname, "status") == "Cancelled":
		frappe.throw(_("Expired Token"))


def get_payment_gateway_controller(payment_gateway):
	"""Return payment gateway controller"""
	gateway = frappe.get_doc("Payment Gateway", payment_gateway)
	if gateway.gateway_controller is None:
		try:
			return frappe.get_doc(f"{payment_gateway} Settings")
		except Exception:
			frappe.throw(_("{0} Settings not found").format(payment_gateway))
	else:
		try:
			return frappe.get_doc(gateway.gateway_settings, gateway.gateway_controller)
		except Exception:
			frappe.throw(_("{0} Settings not found").format(payment_gateway))


# //// Neoffice — upstream wrote one Integration Request per call for any visitor, also on a gateway the site
# //// never set up, with no limit: table growth, and an outbound call on PayPal. A gateway has its `Payment
# //// Gateway` row once its settings are saved: without one the visitor gets the page of a broken
# //// configuration, as for a gateway that fails, and a caller is held to 10 calls a minute.
# permission-check: ok a public door: a checkout link for a gateway this site set up, it reads no record
@frappe.whitelist(allow_guest=True, xss_safe=True)
@rate_limit(limit=10, seconds=60)
def get_checkout_url(**kwargs):
	try:
		payment_gateway = kwargs.get("payment_gateway")
		if not isinstance(payment_gateway, str):
			raise Exception
		if frappe.db.exists("Payment Gateway", payment_gateway):
			doc = frappe.get_doc("{} Settings".format(payment_gateway))
			return doc.get_payment_url(**kwargs)
		else:
			raise Exception
	except Exception:
		frappe.respond_as_web_page(
			_("Something went wrong"),
			_(
				"Looks like something is wrong with this site's payment gateway configuration. No payment has been made."
			),
			indicator_color="red",
			http_status_code=frappe.ValidationError.http_status_code,
		)


def create_payment_gateway(gateway, settings=None, controller=None):
	# NOTE: we don't translate Payment Gateway name because it is an internal doctype
	if not frappe.db.exists("Payment Gateway", gateway):
		payment_gateway = frappe.get_doc(
			{
				"doctype": "Payment Gateway",
				"gateway": gateway,
				"gateway_settings": settings,
				"gateway_controller": controller,
			}
		)
		payment_gateway.insert(ignore_permissions=True)


def make_custom_fields():
	if not frappe.get_meta("Web Form").has_field("payments_tab"):
		click.secho("* Installing Payment Custom Fields in Web Form")

		create_custom_fields(
			{
				"Web Form": [
					{
						"fieldname": "payments_tab",
						"fieldtype": "Tab Break",
						"label": "Payments",
						"insert_after": "custom_css",
					},
					{
						"default": "0",
						"fieldname": "accept_payment",
						"fieldtype": "Check",
						"label": "Accept Payment",
						"insert_after": "payments",
					},
					{
						"depends_on": "accept_payment",
						"fieldname": "payment_gateway",
						"fieldtype": "Link",
						"label": "Payment Gateway",
						"options": "Payment Gateway",
						"insert_after": "accept_payment",
					},
					{
						"default": "Buy Now",
						"depends_on": "accept_payment",
						"fieldname": "payment_button_label",
						"fieldtype": "Data",
						"label": "Button Label",
						"insert_after": "payment_gateway",
					},
					{
						"depends_on": "accept_payment",
						"fieldname": "payment_button_help",
						"fieldtype": "Text",
						"label": "Button Help",
						"insert_after": "payment_button_label",
					},
					{
						"fieldname": "payments_cb",
						"fieldtype": "Column Break",
						"insert_after": "payment_button_help",
					},
					{
						"default": "0",
						"depends_on": "accept_payment",
						"fieldname": "amount_based_on_field",
						"fieldtype": "Check",
						"label": "Amount Based On Field",
						"insert_after": "payments_cb",
					},
					{
						"depends_on": "eval:doc.accept_payment && doc.amount_based_on_field",
						"fieldname": "amount_field",
						"fieldtype": "Select",
						"label": "Amount Field",
						"insert_after": "amount_based_on_field",
					},
					{
						"depends_on": "eval:doc.accept_payment && !doc.amount_based_on_field",
						"fieldname": "amount",
						"fieldtype": "Currency",
						"label": "Amount",
						"insert_after": "amount_field",
					},
					{
						"depends_on": "accept_payment",
						"fieldname": "currency",
						"fieldtype": "Link",
						"label": "Currency",
						"options": "Currency",
						"insert_after": "amount",
					},
				]
			}
		)

		frappe.clear_cache(doctype="Web Form")

	if "erpnext" in frappe.get_installed_apps():
		custom_fields = {
			"GoCardless Mandate": [
				{
					"fieldname": "customer",
					"fieldtype": "Link",
					"in_list_view": 1,
					"label": "Customer",
					"options": "Customer",
					"reqd": 1,
					"insert_after": "disabled",
				}
			]
		}

		create_custom_fields(custom_fields)


def delete_custom_fields():
	if not frappe.get_meta("Web Form").has_field("payments_tab"):
		return

	click.secho("* Uninstalling Payment Custom Fields from Web Form")
	frappe.db.delete(
		"Custom Field",
		{
			"dt": "Web Form",
			"fieldname": (
				"in",
				(
					"payments_tab",
					"accept_payment",
					"payment_gateway",
					"payment_button_label",
					"payment_button_help",
					"payments_cb",
					"amount_field",
					"amount_based_on_field",
					"amount",
					"currency",
				),
			),
		},
	)

	frappe.clear_cache(doctype="Web Form")


def before_install():
	# TODO: remove this
	# This is done for erpnext CI patch test
	#
	# Since we follow a flow like install v14 -> restore v10 site
	# -> migrate to v12, v13 and then v14 again
	#
	# This app fails installing when the site is restored to v10 as
	# a lot of apis don;t exist in v10 and this is a (at the moment) required app for erpnext.
	if not frappe.get_meta("Module Def").has_field("custom"):
		return False


@contextmanager
def erpnext_app_import_guard():
	marketplace_link = '<a href="https://frappecloud.com/marketplace/apps/erpnext">Marketplace</a>'
	github_link = '<a href="https://github.com/frappe/erpnext">GitHub</a>'
	msg = _("erpnext app is not installed. Please install it from {} or {}").format(
		marketplace_link, github_link
	)
	try:
		yield
	except ImportError:
		frappe.throw(msg, title=_("Missing ERPNext App"))
