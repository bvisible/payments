# //// Neoffice — added file (no upstream equivalent). Bench-side helpers for the webshop
# //// checkout runbook, called through `bench execute` (fixtures, assertions,
# //// simulators). Distinct from the Playwright suite next door, which drives Chrome.
# //// Commits: f29b04b 2026-05-20 "test(e2e): webshop E2E framework".
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""End-to-end test helpers for the webshop checkout flow.

These modules support the runbook documented in
``Neoffice/Webshop/Runbooks/E2E-Full-Checkout.md`` (Obsidian vault). They are
invoked via ``bench --site <site> execute payments.tests.e2e.<module>.<fn>``
from the runbook to:

- :mod:`fixtures` — create / reset the fixed Test E2E Webshop Customer +
  Address + User, clean orphaned Quotations / Sales Orders between runs.
- :mod:`assertions` — verify PI/PR/SO triplet state after each PSP run.
- :mod:`simulators` — fake payment success when no real card / app is
  available (gated by ``frappe.conf.enable_e2e_simulators`` for safety).
"""
