# //// Neoffice — added file (no upstream equivalent). The TWINT dialog on a website page never opens
# //// the browser's own confirm()/alert() and asks for its words in English (maintenance#1733).
# Copyright (c) 2026, Neoffice and Contributors
# License: MIT. See LICENSE
"""What `payments/public/js/twint_dialog.js` may not do on the shopper's page. It reads the source: no
browser and no site needed.

The cancel path of the website branch used the browser's `confirm()` and `alert()`, which freeze the whole
tab (a phone's browser may also silence them for the site); Frappe's `frappe.confirm`, `frappe.show_alert`
and `frappe.msgprint` are loaded on website pages too. One title of the Android selector was asked for
under a French msgid, which no catalogue can translate: an English screen showed French.
"""

import re
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
DIALOG = APP / "public" / "js" / "twint_dialog.js"

# `confirm(` / `alert(` / `prompt(` called on their own or on `window`; `frappe.confirm(` and
# `frappe.show_alert(` do not match (a dot or an underscore stands before the name).
NATIVE_DIALOG = re.compile(r"(?<![\w$.])(?:window\.)?(?:confirm|alert|prompt)\s*\(")
MSGID = re.compile(r"""\b__\(\s*(["'])((?:\\.|(?!\1).)*)\1""")
FRENCH_LETTERS = re.compile("[àâäçéèêëîïôöùûüÿœæÀÂÄÇÉÈÊËÎÏÔÖÙÛÜŸŒÆ]")
FRENCH_WORDS = re.compile(
	r"\b(vers|paiement|annuler|annulé|erreur|veuillez|votre|vous|avec|dans|une|les|des)\b", re.I
)


def _code(text: str) -> str:
	"""The source without its comments (a URL such as `https://…` keeps its slashes)."""
	text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
	return re.sub(r"(?<!:)//.*", "", text)


def _msgids(text: str) -> list[str]:
	return [match.group(2) for match in MSGID.finditer(_code(text))]


class TestTheDialogOnAWebsitePage(unittest.TestCase):
	def setUp(self):
		self.source = DIALOG.read_text(encoding="utf-8")

	def test_no_call_to_the_browsers_own_dialogs(self):
		found = [match.group(0) for match in NATIVE_DIALOG.finditer(_code(self.source))]
		self.assertEqual(found, [], "use frappe.confirm / frappe.show_alert / frappe.msgprint")

	def test_the_cancel_of_the_website_branch_asks_and_tells_through_frappe(self):
		code = _code(self.source)
		self.assertGreaterEqual(code.count("frappe.confirm("), 2)  # desk branch and website branch
		self.assertGreaterEqual(code.count("frappe.show_alert("), 3)
		self.assertIn("frappe.msgprint(", code)

	def test_the_native_dialog_pattern_does_catch_what_it_is_meant_to(self):
		for call in ("confirm('x')", "alert(__('x'))", "window.confirm('x')", "if (confirm(msg))"):
			with self.subTest(call=call):
				self.assertTrue(NATIVE_DIALOG.search(call))
		for call in ("frappe.confirm('x')", "frappe.show_alert({})", "window.show_alert(1)"):
			with self.subTest(call=call):
				self.assertFalse(NATIVE_DIALOG.search(call))


class TestTheWordsOfTheDialogAreEnglish(unittest.TestCase):
	"""A msgid is the English source text: the catalogue turns it into the shopper's language."""

	def setUp(self):
		self.source = DIALOG.read_text(encoding="utf-8")

	def test_every_msgid_is_english(self):
		msgids = _msgids(self.source)
		self.assertGreater(len(msgids), 10)
		for msgid in msgids:
			with self.subTest(msgid=msgid):
				self.assertIsNone(FRENCH_LETTERS.search(msgid))
				self.assertIsNone(FRENCH_WORDS.search(msgid))

	def test_the_android_selector_title_is_asked_in_english(self):
		self.assertIn("Redirecting to TWINT", _msgids(self.source))
		self.assertNotIn("Redirection vers TWINT", _msgids(self.source))

	def test_french_has_the_english_msgid_translated(self):
		fr = (APP / "locale" / "fr.po").read_text(encoding="utf-8")
		self.assertRegex(fr, r'msgid "Redirecting to TWINT"\nmsgstr "Redirection vers TWINT"\n')
		self.assertNotIn('msgid "Redirection vers TWINT"', fr)

	def test_the_template_catalogue_asks_for_the_english_msgid_too(self):
		pot = (APP / "locale" / "main.pot").read_text(encoding="utf-8")
		self.assertIn('msgid "Redirecting to TWINT"', pot)
		self.assertNotIn('msgid "Redirection vers TWINT"', pot)
