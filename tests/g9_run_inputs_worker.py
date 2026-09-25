"""Standalone worker for the G9 extension (CLAUDE.md §5 G9, independent
review 2026-09-25 item 1 "run inputs" -- docs/specs/
P7_mapping_authoring_design.md §1.6): run as a real subprocess (never
imported by pytest) so PYTHONHASHSEED genuinely differs between the two
invocations -- see tests/g9_subprocess_worker.py's own docstring for why an
in-process comparison cannot catch this class of nondeterminism.

Runs orchestrator.engine.execute_skill against tests/fixtures/skills/mini,
reading a renamed-column copy of claims.csv through a MappedDataSource
(Employee ID -> Emp No, Amount -> Amt) with register declared not_supplied,
and prints the canonical JSON of every non-narrative output: test_results,
metrics, sorted flags, and findings.

Usage: python tests/g9_run_inputs_worker.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.adapters.mapped_source import MappedDataSource  # noqa: E402
from orchestrator.contract import LocalFileDataSource  # noqa: E402
from orchestrator.engine import execute_skill  # noqa: E402
from orchestrator.skills import load_skill  # noqa: E402

MINI_SKILL_DIR = REPO_ROOT / "tests" / "fixtures" / "skills" / "mini"
CLAIMS_CSV = (
    "Emp No,Transaction Date,Amt,Vendor\n"
    "1,2025-01-05,600,Acme\n"
    "1,2025-01-06,100,Acme\n"
    "2,2025-01-10,900,Beta\n"
)


def main() -> None:
    skill = load_skill(MINI_SKILL_DIR)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "claims.csv").write_text(CLAIMS_CSV)
        inner = LocalFileDataSource(root_dir=tmp_path, sources=skill.contract["sources"])
        mapped = MappedDataSource(inner, {"claims": {"Employee ID": "Emp No", "Amount": "Amt"}})

        result = execute_skill(
            skill, data_source=mapped, audit_period=("2025-01-01", "2025-01-31"),
            run_context={"run_id": "g9-run-inputs"},
            not_supplied={"register": "not held by this business unit"},
        )

    flags_sorted = result.flags.sort_values(list(result.flags.columns)).reset_index(drop=True)
    payload = {
        "test_results": result.test_results,
        "metrics": result.metrics,
        "flags": flags_sorted.to_dict("records"),
        "findings": result.findings,
    }
    print(json.dumps(payload, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
