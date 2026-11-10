# //// Neoffice — added file (no upstream equivalent). The terminals card of « Payment settings »
# //// (2026-11-10): one list of every card terminal, a terminal moved to another till leaves the first,
# //// one taken off leaves every till, and a Payrexx pairing is the last step, after the database.
"""Card terminals at the tills, managed from one list."""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.roles import as_role
from frappe.tests.utils import FrappeTestCase

from payments.api import payrexx_setup, terminals
from payments.setup import provider_setup


class TestTerminalsOneList(FrappeTestCase):
	def setUp(self):
		if not frappe.db.exists("Payment Provider", payrexx_setup.PROVIDER):
			self.skipTest("no Payrexx provider on this site")
		self.tills = frappe.get_all(
			"POS Profile",
			filters={"disabled": 0, "company": frappe.defaults.get_global_default("company")},
			pluck="name",
			limit=2,
		)
		if not self.tills:
			self.skipTest("no till for the default company")
		self._commit = patch.object(frappe.db, "commit")
		self._commit.start()
		self.addCleanup(self._commit.stop)
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")  # the scene only
		if len(self.tills) < 2:
			# A second till of the same shop, gone with the rollback.
			second = frappe.copy_doc(frappe.get_doc("POS Profile", self.tills[0]))
			second.name = second.__newname = "_T Till 2"
			second.set("applicable_for_users", [])
			second.insert(ignore_permissions=True)
			self.tills.append(second.name)
		frappe.db.set_global(payrexx_setup.STATUS_KEY, "{}")
		with as_role("head_accountant"):
			self.prepared = payrexx_setup._prepare_erp()
		self.device = payrexx_setup._ensure_device("_T-N860")

	def test_a_terminal_can_be_shared_by_two_tills_and_taken_off_one(self):
		first, second = self.tills
		with as_role("head_accountant"):
			terminals.attach_terminal(device=self.device, pos_profile=first)
			page = terminals.attach_terminal(device=self.device, pos_profile=second)
		self.assertEqual(provider_setup.terminal_tills(self.device), sorted([first, second]))
		row = next(d for d in page["terminals"]["devices"] if d["name"] == self.device)
		self.assertEqual(row["provider"], payrexx_setup.PROVIDER)
		with as_role("head_accountant"):
			terminals.detach_terminal(device=self.device, pos_profile=first)
		self.assertEqual(provider_setup.terminal_tills(self.device), [second])

	def test_a_terminal_keeps_the_mode_it_was_set_up_with(self):
		with as_role("head_accountant"):
			terminals.attach_terminal(device=self.device, pos_profile=self.tills[0])
		self.assertEqual(provider_setup.terminal_mode(self.device), self.prepared["till_mode"])
		with patch.object(payrexx_setup, "_status", return_value={"till_mode": "_T Another mode"}):
			self.assertEqual(
				terminals._till_mode_of(payrexx_setup.PROVIDER, self.device), self.prepared["till_mode"]
			)

	def test_a_removed_terminal_leaves_every_till_and_the_list(self):
		with as_role("head_accountant"):
			terminals.attach_terminal(device=self.device, pos_profile=self.tills[0])
			page = terminals.remove_terminal(device=self.device)
		self.assertEqual(provider_setup.terminal_tills(self.device), [])
		self.assertFalse(frappe.db.get_value("Payment Device", self.device, "enabled"))
		self.assertNotIn(self.device, [d["name"] for d in page["terminals"]["devices"]])

	def test_one_default_terminal_per_mode_at_a_till(self):
		till = self.tills[0]
		other = payrexx_setup._ensure_device("_T-N600")
		with as_role("head_accountant"):
			terminals.attach_terminal(device=self.device, pos_profile=till)
			terminals.attach_terminal(device=other, pos_profile=till)
		profile = frappe.get_doc("POS Profile", till)
		if not profile.meta.get_field("custom_active_payment_devices"):
			self.skipTest("no active devices on tills here")
		defaults = [
			r.payment_device
			for r in profile.custom_active_payment_devices
			if r.mode_of_payment == self.prepared["till_mode"] and r.is_default
		]
		self.assertEqual(defaults, [other])

	def test_payrexx_pairs_last_and_a_refused_code_stops_the_link(self):
		creds = payrexx_setup._read_credentials(
			frappe.get_doc("Payment Provider", payrexx_setup.PROVIDER)
		)
		creds["webhook_signing_key"] = "_t-key"
		frappe.db.set_value(
			"Payment Provider", payrexx_setup.PROVIDER, "credentials_json", frappe.as_json(creds)
		)
		seen = {}

		def pair(serial, code, cashier_name=None):  # noqa: ANN001, ARG001
			seen["mapped"] = provider_setup.terminal_tills(
				frappe.db.get_value("Payment Device", {"serial_number": serial}, "name")
			)
			return {"paired": False}

		driver = MagicMock(pair_terminal=pair)
		with (
			patch("payments.drivers.registry.resolve_driver", return_value=driver),
			as_role("head_accountant"),
			self.assertRaises(frappe.ValidationError),
		):
			payrexx_setup.link_terminal(
				serial_number="_t-n6", pairing_code="abc123", pos_profile=self.tills[0]
			)
		self.assertEqual(seen["mapped"], [self.tills[0]], "the till was wired before Payrexx was asked")

	def test_a_wallee_terminal_is_picked_from_the_space(self):
		from payments.api import wallee_setup

		provider = wallee_setup._provider_name()
		if not frappe.db.exists(
			"Provider Channel Settings", {"provider": provider, "channel": "terminal"}
		):
			self.skipTest("no Wallee terminal binding on this site")
		with (
			patch.object(
				terminals,
				"wallee_terminals",
				return_value=[{"id": 9_000_001, "name": "_T Desk", "identifier": "_T-1"}],
			),
			patch.object(terminals, "_till_mode_of", return_value=self.prepared["till_mode"]),
			as_role("head_accountant"),
		):
			terminals.add_wallee_terminal(terminal_id="9000001", pos_profile=self.tills[0])
		device = frappe.db.get_value("Payment Device", {"provider_device_id": "9000001"}, "name")
		self.assertTrue(device)
		self.assertEqual(provider_setup.terminal_tills(device), [self.tills[0]])

	def test_a_disabled_mode_never_drives_a_terminal(self):
		mode = self.prepared["till_mode"]
		with as_role("head_accountant"):
			terminals.attach_terminal(device=self.device, pos_profile=self.tills[0])
		self.assertEqual(provider_setup.terminal_mode(self.device), mode)
		self.assertEqual(provider_setup.driven_mode(payrexx_setup.PROVIDER), mode)
		frappe.db.set_value("Mode of Payment", mode, "enabled", 0)
		self.assertIsNone(provider_setup.terminal_mode(self.device))
		self.assertNotEqual(provider_setup.driven_mode(payrexx_setup.PROVIDER), mode)
