from __future__ import annotations

import re
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATOR_DIR = REPO_ROOT / "orchestrator"
APP_DIR = REPO_ROOT / "app"
SCRIPTS_DIR = REPO_ROOT / "scripts"
SKILLS_DIR = REPO_ROOT / "skills"
TESTS_DIR = Path(__file__).resolve().parent
THIS_FILE = Path(__file__).resolve()

# Independent review 2026-09-24 item 19 (F16): the scan below covered only
# *.py in four folders -- it missed every Skill YAML, the DDL, and the PPTX
# template, which is exactly where the audit found real hardcoded
# organisation identifiers (templates/report_template.pptx's docProps and
# an optus.com.au user id in its now-removed changesInfo1.xml -- see
# scripts/strip_pptx_template.py). reference_app/ and docs/ are excluded
# from every scan in this file: the build brief NAMES the organisation
# throughout (CLAUDE.md itself, §0 onward), by design, as the prototype
# this project ports away from -- not a portability violation. A `.env` (or
# `.env.local`, `.env.dev`, ...) is excluded the same way: it is gitignored,
# real per-workspace secrets/identifiers, never committed, and is not this
# scan's job -- `.env.example` stays in scope (it must stay a template with
# no real values of its own).
_EXCLUDED_DIRS = (REPO_ROOT / "reference_app", REPO_ROOT / "docs")


def _is_excluded(path: Path) -> bool:
    if "__pycache__" in path.parts or ".git" in path.parts:
        return True
    if path.name == ".env" or (path.name.startswith(".env") and path.name != ".env.example"):
        return True
    return any(excluded in path.parents or path == excluded for excluded in _EXCLUDED_DIRS)

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


def _repo_files(base: Path, *suffixes: str):
    for suffix in suffixes:
        for path in base.rglob(f"*{suffix}"):
            if _is_excluded(path):
                continue
            yield path


def test_no_hardcoded_workspace_identifiers_in_yaml_sql_json():
    """Skill YAML (contract/plan/thresholds/findings/manifest), DDL, and
    JSON config/fixtures -- none of it was covered by the *.py-only scan
    above, and a Skill's own thresholds.yaml/contract.yaml is exactly where
    a hardcoded catalog/schema/org name would matter most (CLAUDE.md NN16)."""
    offenders = []
    for path in _repo_files(REPO_ROOT, ".yaml", ".yml", ".sql", ".json"):
        text = path.read_text(errors="replace")
        for match in PATTERN.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{line_no}: {match.group(0)!r}")
    assert not offenders, "hardcoded workspace/org identifiers found:\n" + "\n".join(offenders)


def test_no_hardcoded_workspace_identifiers_in_markdown_under_orchestrator_and_skills():
    """Markdown under orchestrator/ (prompt templates) and skills/ (Skill
    docs) -- scoped there, not repo-wide: narrative documentation elsewhere
    (docs/, already excluded above) legitimately discusses the organisation
    by name as the porting target, which is not a violation."""
    offenders = []
    for path in _repo_files(ORCHESTRATOR_DIR, ".md"):
        text = path.read_text(errors="replace")
        for match in PATTERN.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{line_no}: {match.group(0)!r}")
    for path in _repo_files(SKILLS_DIR, ".md"):
        text = path.read_text(errors="replace")
        for match in PATTERN.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{line_no}: {match.group(0)!r}")
    assert not offenders, "hardcoded workspace/org identifiers found:\n" + "\n".join(offenders)


def test_no_hardcoded_workspace_identifiers_in_csv_headers():
    """Only the header row -- a CSV's own DATA body (synthetic claim rows,
    exchange-rate history, ...) is not this scan's concern and can be large;
    a hardcoded catalog/schema/org name belongs in a column name, if
    anywhere, not a data value."""
    offenders = []
    for path in _repo_files(REPO_ROOT, ".csv"):
        with path.open("r", errors="replace") as f:
            header = f.readline()
        for match in PATTERN.finditer(header):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:1 (header): {match.group(0)!r}")
    assert not offenders, "hardcoded workspace/org identifiers found in a CSV header:\n" + "\n".join(offenders)


# Independent review 2026-09-24 items 14/19 (D13/F16): docProps (core/app/
# custom -- author, manager, a stale TitlesOfParts list) plus a document's
# own CONTENT xml, never a template's visual design. Slide MASTERS/LAYOUTS/
# theme are excluded on purpose: scripts/strip_pptx_template.py's own
# docstring is explicit that an organisation name baked into the template's
# own branding (a footer, an Acknowledgement of Country) is reported, not
# removed -- a portability scan that turned red over that text would be
# asking this script to do the one thing it was told not to do.
_OFFICE_CONTENT_XML_PREFIXES = ("ppt/slides/", "xl/worksheets/", "xl/sharedStrings")
_OFFICE_CONTENT_XML_EXACT = ("word/document.xml",)

# CONTENT (never docProps -- see below) is scanned only for office documents
# this project itself produces or ships as a template -- never the
# synthetic AUDIT DATA fixtures, which simulate one specific organisation's
# real expense/travel data ON PURPOSE (CLAUDE.md §11's SKILL-001 decisions)
# and so legitimately carry that organisation's name as DATA, in every row,
# the same reason reference_app/ and docs/ are excluded above. A stray
# identifier in a data file's own METADATA (its docProps -- who last opened
# it, what tool wrote it) is still a real leak and stays in scope.
_DATA_DIRS_EXCLUDED_FROM_CONTENT_SCAN = (REPO_ROOT / "synthetic_data", REPO_ROOT / "tests" / "fixtures")


def test_no_hardcoded_workspace_identifiers_in_office_document_package_xml():
    offenders = []
    for path in _repo_files(REPO_ROOT, ".pptx", ".xlsx", ".docx"):
        is_data_fixture = any(
            data_dir in path.parents for data_dir in _DATA_DIRS_EXCLUDED_FROM_CONTENT_SCAN
        )
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if not name.endswith(".xml"):
                    continue
                is_doc_props = name.startswith("docProps/")
                is_content = name.startswith(_OFFICE_CONTENT_XML_PREFIXES) or name in _OFFICE_CONTENT_XML_EXACT
                if not is_doc_props and not (is_content and not is_data_fixture):
                    continue
                text = z.read(name).decode("utf-8", errors="replace")
                for match in PATTERN.finditer(text):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}!{name}: {match.group(0)!r}")
    assert not offenders, (
        "hardcoded workspace/org identifiers found in an office document's own package XML:\n"
        + "\n".join(offenders)
    )
