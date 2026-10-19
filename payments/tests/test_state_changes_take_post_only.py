# //// Neoffice — added file (no upstream equivalent). maintenance#1721: every whitelisted function of our payment
# //// modules that changes state, mints a secret or moves money takes POST only; only the reads named below stay open.
"""Whitelisted payment functions that change something refuse a GET.

Frappe checks no CSRF token on a GET and rolls its transaction back. A refund opened as an address returned the
money at TWINT and left the Payment Intent `succeeded`, refundable a second time (osiris, 19.10). So a function
that writes here, acts at a PSP, prints, mails or mints a secret is never reachable by GET. The sweep reads every
whitelisted function these modules define: a new one is either POST only or named in OPEN_TO_GET with its reason.
"""

import importlib

import frappe
from frappe.tests.utils import FrappeTestCase

MODULES = (
	"payments.api.intent",
	"payments.api.mobile_payments",
	"payments.api.payrexx_setup",
	"payments.api.terminal",
	"payments.api.terminal_receipt",
	"payments.api.twint",
	"payments.integrations.payrexx.api",
	"payments.integrations.twint.api",
	"payments.integrations.wallee.api",
	"payments.payments.page.payment_terminal_wizard.payment_terminal_wizard",
)

# Reads, plus one poll: the phones already installed call mobile_refresh_status with GET, and it only copies
# Stripe's verdict onto an open card intent.
OPEN_TO_GET = {
	"payments.api.intent": {"get_intent_status"},
	"payments.api.mobile_payments": {
		"mobile_context",
		"mobile_recent_payments",
		"mobile_payments_for",
		"mobile_refresh_status",
		"mobile_paid_total",
		"mobile_receipt",
	},
	"payments.api.payrexx_setup": {"get_current_setup", "test_connection"},
	"payments.api.terminal": {"list_stripe_locations", "list_stripe_readers"},
	"payments.api.terminal_receipt": {"preview_text", "preview_receipt"},
	"payments.api.twint": set(),
	"payments.integrations.payrexx.api": {"get_intent_state", "is_payrexx_enabled"},
	"payments.integrations.twint.api": {"get_intent_state", "get_ios_schemes"},
	"payments.integrations.wallee.api": {
		"test_connection",
		"get_existing_locations",
		"get_existing_configurations",
		"get_lightbox_javascript_url",
		"get_iframe_javascript_url",
		"get_payment_method_configurations",
		"get_terminal_types",
	},
	"payments.payments.page.payment_terminal_wizard.payment_terminal_wizard": {
		"list_terminal_providers",
		"list_pos_profiles",
		"list_modes_of_payment_for_profile",
	},
}

# Named so that a sweep that silently found nothing (a module renamed, a decorator changed) cannot pass.
POST_ONLY = {
	"payments.api.intent": {"create_intent", "cancel_intent", "refund_intent"},
	"payments.api.mobile_payments": {
		"connection_token",
		"mobile_start_payment",
		"mobile_abandon_payment",
		"simulate_success",
		"send_receipt",
	},
	"payments.api.payrexx_setup": {"save_credentials", "setup_channels", "setup_tiles"},
	"payments.api.terminal": {
		"create_stripe_location",
		"ensure_stripe_webhook",
		"register_stripe_reader",
		"attach_intent_to_reader",
	},
	"payments.api.terminal_receipt": {"print_receipt"},
	"payments.api.twint": {"simulate_consumer_success"},
	"payments.integrations.payrexx.api": {"create_web_transaction"},
	"payments.integrations.twint.api": {"create_web_transaction", "cancel_web_transaction"},
	"payments.integrations.wallee.api": {
		"sync_locations_from_wallee",
		"sync_configurations_from_wallee",
		"create_terminal",
		"link_terminal_device",
		"create_web_transaction",
	},
	"payments.payments.page.payment_terminal_wizard.payment_terminal_wizard": {
		"link_device_to_pos_profile"
	},
}


def _whitelisted(module_name: str) -> dict:
	"""{function name: allowed HTTP methods} for the whitelisted functions the module itself defines."""
	module = importlib.import_module(module_name)
	allowed = frappe.allowed_http_methods_for_whitelisted_func
	return {
		name: tuple(allowed[obj])
		for name, obj in vars(module).items()
		if callable(obj) and getattr(obj, "__module__", None) == module_name and obj in allowed
	}


class TestStateChangesTakePostOnly(FrappeTestCase):
	def test_what_changes_state_refuses_a_get(self):
		for module_name in MODULES:
			for name, methods in _whitelisted(module_name).items():
				if name in OPEN_TO_GET[module_name]:
					continue
				with self.subTest(function=f"{module_name}.{name}"):
					self.assertEqual(methods, ("POST",))

	def test_the_sweep_sees_every_named_function(self):
		for module_name in MODULES:
			found = _whitelisted(module_name)
			with self.subTest(module=module_name):
				self.assertLessEqual(POST_ONLY[module_name] | OPEN_TO_GET[module_name], set(found))
				self.assertEqual({n for n, m in found.items() if m == ("POST",)}, POST_ONLY[module_name])

	def test_the_reads_stay_open_to_a_get(self):
		for module_name in MODULES:
			found = _whitelisted(module_name)
			for name in OPEN_TO_GET[module_name]:
				with self.subTest(function=f"{module_name}.{name}"):
					self.assertIn("GET", found[name])
