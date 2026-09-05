# //// Neoffice — added file (no upstream equivalent). Exists for one reason worth knowing: the
# //// scenario modules import their siblings as top-level names (`from helpers import
# //// …`), which pytest resolves through its rootdir but frappe's runner does not —
# //// and frappe imports every `test_*.py` of the app in ONE process, so those imports
# //// killed `bench run-tests --app payments` before any unit test ran. This puts the
# //// directory on sys.path; the modules define pytest functions, not unittest cases,
# //// so the runner still collects nothing from them.
# //// Commits: 187b5c8 2026-05-20; f2417b2 2026-09-03 "make the playwright package
# //// importable by frappe's test runner".
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Playwright e2e suite — run with pytest from this directory (see README.md).

The modules import their siblings as top-level names (``from helpers import …``),
which pytest resolves through its rootdir. frappe's test runner imports every
test_*.py of the app in one process, so under ``bench run-tests`` those imports
failed before a single unit test ran (CI, 2026-09-03). Putting this directory on
sys.path makes the modules importable there too; they define pytest functions,
not unittest cases, so the runner collects nothing from them.
"""

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
	sys.path.insert(0, _here)
