"""Run as a subprocess (see test_layout_parity.py): renders one prototype
page (reference_app/src/platform/pages.py) with the prototype's own
fixtures, in isolation from app/'s own `src.*` modules (both trees use the
same dotted module names, so they cannot coexist in one process), and
prints its (depth, tag, id, className) shape, one per line -- the same
format tree.py's `render()` produces for app/'s own pages."""

from __future__ import annotations

import sys

_REPO_ROOT = sys.argv[1]
_PAGE = sys.argv[2]

sys.path.insert(0, f"{_REPO_ROOT}/reference_app")
sys.path.insert(0, f"{_REPO_ROOT}/app/tests")

from tree import shape, render  # noqa: E402
from src.platform import pages  # noqa: E402

if _PAGE == "landing":
    component = pages.landing_page()
elif _PAGE == "skills":
    component = pages.skill_library_page()
elif _PAGE == "runs":
    component = pages.audit_runs_page()
elif _PAGE == "trace":
    component = pages.platform_trace_page()
elif _PAGE == "actions":
    component = pages.management_actions_page()
elif _PAGE == "methodology":
    component = pages.skill_methodology_page("SKILL-001")
else:
    raise SystemExit(f"unknown page {_PAGE!r}")

print(render(shape(component)))
