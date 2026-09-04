#//// Neoffice — added file (no upstream equivalent). Creates the EMV block a card receipt
#//// is printed from on the sites that already exist. It only delegates: the block is
#//// specified in `payments.setup.card_receipt_fields`, which `after_install` runs on a
#//// fresh site, where a patch never runs (`bench install-app` marks them all completed).
#//// Needed on existing sites too: the fields were posted by hand there, so a site is as
#//// likely to be missing them as a new one — that is what issue #192 measured.
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Create the card receipt fields on ``Sales Invoice Payment`` on the sites that exist.

Their specification lives in :mod:`payments.setup.card_receipt_fields`, where
``after_install`` provisions them on a fresh site (a patch never runs there: ``bench
install-app`` marks it as completed). Idempotent — safe to re-run, and a no-op on a
site that already carries the block. Creates no data: an existing payment row keeps
the mentions it has, and a row that has none stays empty until a terminal fills it.
"""

from __future__ import annotations

from payments.setup.card_receipt_fields import provision_card_receipt_fields


def execute() -> None:
	provision_card_receipt_fields()
