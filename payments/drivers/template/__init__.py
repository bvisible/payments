#//// Neoffice — added file (no upstream equivalent). Skeleton package to copy
#//// when adding a PSP to the driver layer (`cp -r payments/drivers/template
#//// payments/drivers/<psp>`); the walkthrough is docs/adding-a-new-psp.md.
#//// Commits: 7cfe7fa 2026-05-13 "docs(payments): Phase 7 runbooks + Phase 8 PSP template"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Driver template — copy this directory as a starting point for new PSPs.

Usage:

    cp -r payments/drivers/template payments/drivers/<psp_lowercase>
    # rename classes inside, implement the abstract methods
    # update `payments/drivers/<psp>/__init__.py` to expose your classes

See `docs/adding-a-new-psp.md` for the full walkthrough.
"""
