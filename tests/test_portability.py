from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATOR_DIR = REPO_ROOT / "orchestrator"
APP_DIR = REPO_ROOT / "app"
SCRIPTS_DIR = REPO_ROOT / "scripts"
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

# "tne_source" is a legitimate, deliberately fake schema name used all over
# app/tests and tests/ fixtures (e.g. "test_catalog.tne_source.expense_report")
# -- not a real workspace identifier, so it does not belong in PATTERN above
# (which would flag every one of those fixtures). scripts/, however, is
# deploy-time code that must never hardcode a real source-schema NAME at
# all (CLAUDE.md §3 non-negotiable 16) -- deploy_app.py used to hardcode
# exactly "tne_source" as the App SP's granted source schema, fixed to read
# DBX_SOURCE_SCHEMAS from config instead (independent review 2026-09-24).
# Scoped to scripts/ only, and to this one literal, not folded into the
# broad fixture-heavy scan above.
SCRIPTS_PATTERN = re.compile(r"\btne_source\b", re.IGNORECASE)


def _python_files(base: Path):
    for path in base.rglob("*.py"):
        if path.resolve() == THIS_FILE or "__pycache__" in path.parts:
            continue
        yield path


def test_no_hardcoded_workspace_identifiers_in_orchestrator_and_tests():
    offenders = []
    for base in (ORCHESTRATOR_DIR, APP_DIR, TESTS_DIR, SCRIPTS_DIR):
        for path in _python_files(base):
            text = path.read_text(errors="replace")
            for match in PATTERN.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{line_no}: {match.group(0)!r}")
    assert not offenders, "hardcoded workspace/org identifiers found:\n" + "\n".join(offenders)


def test_no_hardcoded_source_schema_names_in_scripts():
    offenders = []
    for path in _python_files(SCRIPTS_DIR):
        text = path.read_text(errors="replace")
        for match in SCRIPTS_PATTERN.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{line_no}: {match.group(0)!r}")
    assert not offenders, (
        "scripts/ must read the source schema to grant from config (DBX_SOURCE_SCHEMAS), "
        "never hardcode one:\n" + "\n".join(offenders)
    )
