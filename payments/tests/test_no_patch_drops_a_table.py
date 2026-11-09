# //// Neoffice — added file (no upstream equivalent). neoffice-maintenance#2390: v15_03 and v15_04
# //// dropped the tables of the apps they absorbed, and a shop lost a year of TWINT web transactions
# //// with no backup left. The data of a retired app stays: no patch drops or truncates a table.
"""No patch of payments drops, truncates or uninstalls anything that holds data."""

import ast
import re
from pathlib import Path

from frappe.tests.utils import FrappeTestCase

PATCHES = Path(__file__).resolve().parents[1] / "patches"
DESTROYS = (
	re.compile(r"\bdrop\s+table\b", re.I),
	re.compile(r"\btruncate\s+(table\b|`)", re.I),
	re.compile(r"\buninstall-app\b"),
	re.compile(r"\bremove_app\s*\("),
)


def executable_strings(source: str):
	"""Every string of a module but its docstrings: a comment is not in the tree, a docstring may name what a
	patch must not do."""
	tree = ast.parse(source)
	docstrings = {
		id(node.value)
		for node in ast.walk(tree)
		if isinstance(node, ast.Expr)
		and isinstance(node.value, ast.Constant)
		and isinstance(node.value.value, str)
	}
	for node in ast.walk(tree):
		if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
			yield node.lineno, node.value


class TestNoPatchDropsATable(FrappeTestCase):
	def test_no_patch_drops_truncates_or_uninstalls(self):
		found = []
		for path in sorted(PATCHES.rglob("*.py")):
			if path.name.startswith("test_"):
				continue
			for line, text in executable_strings(path.read_text(encoding="utf-8")):
				if any(pattern.search(text) for pattern in DESTROYS):
					found.append(f"{path.relative_to(PATCHES.parent)}:{line}: {text.strip()[:120]}")
		self.assertEqual(found, [], "\n".join(found))

	def test_the_guard_sees_a_drop(self):
		source = 'X = "DROP TABLE IF EXISTS `tabWallee Transaction`"\n"""Never DROP TABLE here."""\n'
		texts = [text for _, text in executable_strings(source)]
		self.assertEqual(len(texts), 1)
		self.assertTrue(DESTROYS[0].search(texts[0]))
