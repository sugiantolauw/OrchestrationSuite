"""Standalone worker for the L0.2 fieldwork/lifecycle isolation check
(LIFECYCLE_design.md §1 LI-5, §2.4; CLAUDE.md §3 non-negotiable 1's NN1
exception confinement). Run as a real subprocess (never imported by pytest)
so this process's `sys.modules` reflects only what THIS script itself
imports -- never whatever another test module happened to import first in
the same pytest session.

Snapshots `sys.modules` right after importing `orchestrator.nodes.registry`
alone (proving that import by itself does not pull in
`orchestrator.nodes.fieldwork`), then resolves every phase's node functions
for run_kind="fieldwork" through `NODES_FOR` -- the same lazy-resolution
access pattern `orchestrator.pipeline.run_phase` uses for a real run -- and
snapshots `sys.modules` again. Writes both snapshots (filtered to
"orchestrator" and "orchestrator.*" module names) to a JSON file.

Usage: python tests/lifecycle_isolation_worker.py <output_json_path>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import orchestrator.nodes.registry as registry  # noqa: E402


def _orchestrator_modules() -> list[str]:
    return sorted(m for m in sys.modules if m == "orchestrator" or m.startswith("orchestrator."))


if __name__ == "__main__":
    out_path = Path(sys.argv[1])
    before = _orchestrator_modules()

    for phase in ("plan", "execute", "export"):
        _ = registry.NODES_FOR["fieldwork"][phase]

    after = _orchestrator_modules()
    out_path.write_text(json.dumps({"before": before, "after": after}))
