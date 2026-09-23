from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATOR_DIR = REPO_ROOT / "orchestrator"
TESTS_DIR = Path(__file__).resolve().parent
THIS_FILE = Path(__file__).resolve()

PATTERN = re.compile(
    r"dvlp_11|ia_dart|sdpt_gia|optus|adb-[0-9]|dbc-[0-9a-f]{8}|test_workspace|audit_ledger",
    re.IGNORECASE,
)


def _python_files(base: Path):
    for path in base.rglob("*.py"):
        if path.resolve() == THIS_FILE:
            continue
        yield path


def test_no_hardcoded_workspace_identifiers_in_orchestrator_and_tests():
    offenders = []
    for base in (ORCHESTRATOR_DIR, TESTS_DIR):
        for path in _python_files(base):
            text = path.read_text(errors="replace")
            for match in PATTERN.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{line_no}: {match.group(0)!r}")
    assert not offenders, "hardcoded workspace/org identifiers found:\n" + "\n".join(offenders)
