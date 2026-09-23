"""Run as a subprocess (see test_layout_parity.py): prints the prototype's
own fixture data (reference_app/src/platform/fixtures.py) as JSON, so the
main test process can render app/'s pages against the SAME data volume as
the prototype without ever importing both `src.*` trees in one process."""

from __future__ import annotations

import json
import sys

_REPO_ROOT = sys.argv[1]

sys.path.insert(0, f"{_REPO_ROOT}/reference_app")

from src.platform import fixtures  # noqa: E402

print(json.dumps({
    "DEMO_SKILLS": fixtures.DEMO_SKILLS,
    "DEMO_DATA_ASSETS": fixtures.DEMO_DATA_ASSETS,
    "DEMO_AUDIT_RUNS": fixtures.DEMO_AUDIT_RUNS,
    "DEMO_TRACE_EVENTS": fixtures.DEMO_TRACE_EVENTS,
    "DEMO_MANAGEMENT_ACTIONS": fixtures.DEMO_MANAGEMENT_ACTIONS,
}))
