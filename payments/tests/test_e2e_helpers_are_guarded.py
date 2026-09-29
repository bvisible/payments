# //// Neoffice — added file (no upstream equivalent). The end-to-end helpers of `payments.tests.e2e`
# //// are `@frappe.whitelist()` endpoints that ship with the app to every instance; they answered any
# //// signed-in account (#952). These tests need no site.
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""The end-to-end helpers answer a System Manager on a site with the simulators on, and nobody else.

Found while fixing #778: `cleanup_b2b_user(email)` deleted any user and their customer for any signed-in
caller, `assign_customer_to_b2b` moved a customer to the B2B group after `ensure_b2b_environment` had made
the discount rule that goes with it, `assert_payment_complete` read any Payment Intent, and
`get_e2e_site_config` handed out the test password where the site keeps one.

The first class reads the source, so a new helper cannot be added without its guard. The second calls each
real helper with Frappe replaced by stand-ins and checks that a refused caller never reaches the database.
"""

from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path
from unittest import mock

import frappe

from payments.tests.e2e import assertions, fixtures, guard

MODULES = (fixtures, assertions)


def _is_whitelist(decorator: ast.expr) -> bool:
	target = decorator.func if isinstance(decorator, ast.Call) else decorator
	return isinstance(target, ast.Attribute) and target.attr == "whitelist"


def _guard_call(node: ast.FunctionDef) -> ast.Call | None:
	"""The `e2e_only(...)` call that opens the function (after its docstring), or None."""
	body = list(node.body)
	if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
		body = body[1:]
	if not body or not isinstance(body[0], ast.Expr) or not isinstance(body[0].value, ast.Call):
		return None
	call = body[0].value
	return call if isinstance(call.func, ast.Name) and call.func.id == "e2e_only" else None


def unguarded(source: str) -> list[str]:
	"""The whitelisted functions of `source` that do not open with the guard."""
	tree = ast.parse(source)
	return [
		node.name
		for node in tree.body
		if isinstance(node, ast.FunctionDef)
		and any(_is_whitelist(d) for d in node.decorator_list)
		and _guard_call(node) is None
	]


def _helpers() -> list[tuple[str, object]]:
	names = []
	for module in MODULES:
		tree = ast.parse(Path(module.__file__).read_text())
		for node in tree.body:
			if isinstance(node, ast.FunctionDef) and any(_is_whitelist(d) for d in node.decorator_list):
				names.append((node.name, getattr(module, node.name)))
	return names


class TestEveryHelperOpensWithTheGuard(unittest.TestCase):
	def test_no_whitelisted_helper_is_left_unguarded(self):
		for module in MODULES:
			self.assertEqual(unguarded(Path(module.__file__).read_text()), [], module.__name__)

	def test_the_check_would_have_caught_the_old_helpers(self):
		old = "@frappe.whitelist()\ndef cleanup_b2b_user(email):\n\t'''Delete the user.'''\n\tfrappe.delete_doc('User', email)\n"
		self.assertEqual(unguarded(old), ["cleanup_b2b_user"])

	def test_a_guard_that_is_not_first_does_not_count(self):
		late = "@frappe.whitelist()\ndef reset():\n\tfrappe.db.delete('X')\n\te2e_only()\n"
		self.assertEqual(unguarded(late), ["reset"])

	def test_the_helpers_are_found(self):
		# 11 in fixtures.py, 3 in assertions.py at the time of the fix: a parser that finds none proves nothing.
		self.assertGreaterEqual(len(_helpers()), 14)

	def test_only_the_config_reader_answers_a_site_with_the_simulators_off(self):
		relaxed = []
		for module in MODULES:
			tree = ast.parse(Path(module.__file__).read_text())
			for node in tree.body:
				if isinstance(node, ast.FunctionDef) and any(_is_whitelist(d) for d in node.decorator_list):
					call = _guard_call(node)
					if any(k.arg == "needs_simulators" for k in call.keywords):
						relaxed.append(node.name)
		self.assertEqual(relaxed, ["get_e2e_site_config"])


class _NoDatabase:
	"""Stands in for `frappe.db`: any use of it fails the test."""

	def __getattr__(self, name):
		raise AssertionError(f"the database was reached: frappe.db.{name}")


class TestARefusedCallerReachesNothing(unittest.TestCase):
	def _call(self, function, *, system_manager, simulators):
		"""Call the real helper with a fake session, fake site_config and a database that fails on any use."""
		database = _NoDatabase()

		def only_for(role):
			if not system_manager:
				raise frappe.PermissionError("not a System Manager")

		arguments = {
			name: ("someone@example.com" if parameter.annotation in ("str", str) else 1)
			for name, parameter in inspect.signature(function).parameters.items()
			if parameter.default is inspect.Parameter.empty
		}
		with (
			mock.patch.object(frappe, "only_for", side_effect=only_for),
			mock.patch.object(frappe, "conf", frappe._dict(enable_e2e_simulators=simulators)),
			mock.patch.object(frappe, "throw", side_effect=frappe.PermissionError("refused")),
			mock.patch.object(guard, "_", lambda text: text),
			mock.patch.object(frappe, "db", database),
		):
			return getattr(function, "__wrapped__", function)(**arguments)

	def test_a_caller_who_is_not_a_system_manager_is_refused_by_every_helper(self):
		for name, function in _helpers():
			with self.subTest(helper=name), self.assertRaises(frappe.PermissionError):
				self._call(function, system_manager=False, simulators=True)

	def test_a_system_manager_is_refused_on_a_site_with_the_simulators_off(self):
		for name, function in _helpers():
			if name == "get_e2e_site_config":
				continue
			with self.subTest(helper=name), self.assertRaises(frappe.PermissionError):
				self._call(function, system_manager=True, simulators=False)

	def test_the_site_config_reader_still_answers_a_system_manager_with_the_simulators_off(self):
		answer = self._call(fixtures.get_e2e_site_config, system_manager=True, simulators=False)
		self.assertFalse(answer["enable_e2e_simulators"])

	def test_the_site_config_reader_refuses_the_others_even_so(self):
		with self.assertRaises(frappe.PermissionError):
			self._call(fixtures.get_e2e_site_config, system_manager=False, simulators=False)

	def test_the_guard_lets_a_system_manager_through_when_the_simulators_are_on(self):
		with (
			mock.patch.object(frappe, "only_for"),
			mock.patch.object(frappe, "conf", frappe._dict(enable_e2e_simulators=1)),
		):
			self.assertIsNone(guard.e2e_only())


if __name__ == "__main__":
	unittest.main()
