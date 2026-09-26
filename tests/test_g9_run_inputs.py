"""G9 extension (CLAUDE.md §5 G9, independent review 2026-09-25 item 1 "run
inputs"): the same cross-process determinism proof as
tests/test_p3_tne_gates.py's SKILL-001 G9 gate, but for a run that exercises
both a column mapping (MappedDataSource) and an unsupplied source --
tests/g9_run_inputs_worker.py runs execute_skill against
tests/fixtures/skills/mini with both applied, twice, in real subprocesses
with a different PYTHONHASHSEED, and this asserts byte-identical output."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKER = REPO_ROOT / "tests" / "g9_run_inputs_worker.py"


def _run(seed: str) -> str:
    env = {"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin:/usr/local/bin"}
    result = subprocess.run(
        [sys.executable, str(WORKER)], capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_mapped_and_unsupplied_run_is_byte_identical_across_hash_seeds():
    out1 = _run("1")
    out2 = _run("2")
    assert out1 == out2
    assert '"RF_MISSING": null' in out1  # T2 (register-dependent) is not_testable, null not 0
    assert "not held by this business unit" in out1
    assert '"value": 1500.0' in out1  # the mapped Amount column was read correctly
