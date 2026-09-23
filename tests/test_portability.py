from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATOR_DIR = REPO_ROOT / "orchestrator"
APP_DIR = REPO_ROOT / "app"
TESTS_DIR = Path(__file__).resolve().parent
THIS_FILE = Path(__file__).resolve()

# Word-bounded (CLAUDE.md P2/P3 gate review item 4, extending this scan to
# app/): unbounded "test_workspace"/"audit_ledger" substrings also match
# ordinary identifiers like `test_workspace_layout` or
# `test_workspace_tne_renders_...` -- real app/tests function names that
# have nothing to do with the hardcoded `DBX_CATALOG=test_workspace` /
# `DBX_SCHEMA=audit_ledger` values this test exists to catch. Bounding every
# alternative only makes the match stricter, never weaker, against the
# orchestrator/tests files this already passed on.
PATTERN = re.compile(
    r"\b(?:dvlp_11|ia_dart|sdpt_gia|optus|adb-[0-9]|dbc-[0-9a-f]{8}|test_workspace|audit_ledger)\b",
    re.IGNORECASE,
)


def _python_files(base: Path):
    for path in base.rglob("*.py"):
        if path.resolve() == THIS_FILE or "__pycache__" in path.parts:
            continue
        yield path


def test_no_hardcoded_workspace_identifiers_in_orchestrator_and_tests():
    offenders = []
    for base in (ORCHESTRATOR_DIR, APP_DIR, TESTS_DIR):
        for path in _python_files(base):
            text = path.read_text(errors="replace")
            for match in PATTERN.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{line_no}: {match.group(0)!r}")
    assert not offenders, "hardcoded workspace/org identifiers found:\n" + "\n".join(offenders)
