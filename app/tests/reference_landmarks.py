"""Run as a subprocess (see test_layout_parity.py's matching-volume harness,
CLAUDE.md item E): prints one prototype page's landmarks() signature as
JSON. Separate from reference_tree.py because /workspace/tne's prototype
equivalent (tne_workspace_layout()) lives in reference_app/app.py itself,
which computes its demo data at import time (falling back to seeded random
data when the real source files are not reachable, exactly as build brief
§0.2 describes) -- a much heavier import than reference_tree.py's
src.platform.pages alone, so it is kept out of that module's fast path."""

from __future__ import annotations

import json
import sys

_REPO_ROOT = sys.argv[1]
_PAGE = sys.argv[2]

sys.path.insert(0, f"{_REPO_ROOT}/reference_app")
sys.path.insert(0, f"{_REPO_ROOT}/app/tests")

from landmarks import landmarks  # noqa: E402

if _PAGE == "workspace_tne":
    import app as reference_app_module  # reference_app/app.py

    component = reference_app_module.tne_workspace_layout()
elif _PAGE == "methodology":
    from src.platform import pages

    component = pages.skill_methodology_page("SKILL-001")
else:
    raise SystemExit(f"unknown page {_PAGE!r}")

print(json.dumps(landmarks(component)))
