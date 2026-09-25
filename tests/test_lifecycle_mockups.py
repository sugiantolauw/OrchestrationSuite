"""Layer-0 checks on the lifecycle M0 clickable mockups (docs/mockups/lifecycle/,
LIFECYCLE_design.md §6.3's "Mockup deliverable (M0)"). These are static HTML
files for the user's review -- nothing here is wired to app/, so this test
only asserts the deliverable's own shape: every screen exists, every internal
link resolves to a real file in the same folder, no external URLs, no
forbidden workspace/org identifiers (reusing test_portability's own pattern),
and every page carries the MOCKUP ribbon and the one approved nav addition
(LD1 "Audit Lifecycle" -> /lifecycle).
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tests.test_portability import PATTERN as PORTABILITY_PATTERN

REPO_ROOT = Path(__file__).resolve().parent.parent
MOCKUP_DIR = REPO_ROOT / "docs" / "mockups" / "lifecycle"

# LIFECYCLE_design.md §6.3 screen inventory S1-S17. S5, S9 and S14 each cover
# two routes (a list and a detail/run view), so their filename lists have two
# entries -- every route in the table still has its own HTML file.
EXPECTED_SCREENS = {
    "S1": ["S01_lifecycle_hub.html"],
    "S2": ["S02_engagements.html"],
    "S3": ["S03_engagement_home.html"],
    "S4": ["S04_risk_register.html"],
    "S5": ["S05_sensing.html", "S05_sensing_run.html"],
    "S6": ["S06_risk_detail.html"],
    "S7": ["S07_assessment.html"],
    "S8": ["S08_planning.html"],
    "S9": ["S09_templates.html", "S09_template_detail.html"],
    "S10": ["S10_design.html"],
    "S11": ["S11_issues.html"],
    "S12": ["S12_reports.html"],
    "S13": ["S13_leadership.html"],
    "S14": ["S14_monitoring.html", "S14_monitor_detail.html"],
    "S15": ["S15_evidence.html"],
    "S16": ["S16_knowledge.html"],
    "S17": ["S17_run_kind_sections.html"],
}

ALL_EXPECTED_FILES = [f for files in EXPECTED_SCREENS.values() for f in files] + ["index.html", "theme.css"]

HREF_RE = re.compile(r'\shref="([^"]*)"')
SRC_RE = re.compile(r'\ssrc="([^"]*)"')


def _html_files() -> list[Path]:
    return sorted(MOCKUP_DIR.glob("*.html"))


def test_mockup_directory_exists():
    assert MOCKUP_DIR.is_dir(), f"{MOCKUP_DIR} does not exist -- the M0 mockups were not generated"


@pytest.mark.parametrize("screen,files", sorted(EXPECTED_SCREENS.items()))
def test_every_inventory_screen_has_a_file(screen, files):
    for f in files:
        path = MOCKUP_DIR / f
        assert path.is_file(), f"{screen}: expected mockup file {f!r} is missing"


def test_theme_css_present_and_is_a_copy_of_the_real_stylesheet():
    mock_css = MOCKUP_DIR / "theme.css"
    real_css = REPO_ROOT / "app" / "assets" / "theme.css"
    assert mock_css.is_file(), "docs/mockups/lifecycle/theme.css is missing"
    assert mock_css.read_text() == real_css.read_text(), (
        "docs/mockups/lifecycle/theme.css has drifted from app/assets/theme.css -- "
        "the task requires an exact copy, never a modified one"
    )


def test_no_stray_files_beyond_the_inventory():
    present = {p.name for p in MOCKUP_DIR.iterdir() if p.is_file()}
    unexpected = present - set(ALL_EXPECTED_FILES)
    assert not unexpected, f"unexpected files in {MOCKUP_DIR}: {sorted(unexpected)}"


def _internal_links(html: str) -> list[str]:
    return HREF_RE.findall(html) + SRC_RE.findall(html)


def test_every_internal_link_resolves_to_an_existing_file():
    known = {p.name for p in _html_files()} | {"theme.css"}
    offenders = []
    for path in _html_files():
        html = path.read_text()
        for link in _internal_links(html):
            if link in ("", "#"):
                continue
            parsed = urlsplit(link)
            if parsed.scheme or parsed.netloc:
                # external URLs are checked separately below
                continue
            target = parsed.path
            if not target:
                # a pure fragment/query link, e.g. "#overview" or "?eid=ENG-FY"
                continue
            if target not in known:
                offenders.append(f"{path.name}: link to {link!r} (resolves to missing file {target!r})")
    assert not offenders, "broken internal link(s):\n" + "\n".join(offenders)


def test_no_external_urls():
    offenders = []
    for path in _html_files() + [MOCKUP_DIR / "theme.css"]:
        text = path.read_text()
        for m in re.finditer(r'\b(?:href|src)="(https?://[^"]*)"', text):
            offenders.append(f"{path.name}: external URL {m.group(1)!r}")
        for m in re.finditer(r"https?://\S+", text):
            offenders.append(f"{path.name}: external URL-looking text {m.group(0)!r}")
    assert not offenders, "external URL(s) found (mockups must work offline):\n" + "\n".join(offenders)


def test_no_forbidden_workspace_or_organisation_identifiers():
    """Reuses tests/test_portability.py's own PATTERN (CLAUDE.md NN16) --
    the mockups describe a generic "the Company", never a real org, catalog,
    workspace or host."""
    offenders = []
    for path in _html_files():
        text = path.read_text()
        for match in PORTABILITY_PATTERN.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{line_no}: {match.group(0)!r}")
    assert not offenders, "hardcoded workspace/org identifiers found in the mockups:\n" + "\n".join(offenders)


def test_every_page_has_the_mockup_ribbon():
    for path in _html_files():
        text = path.read_text()
        assert "MOCKUP" in text and "synthetic data, not built" in text, (
            f"{path.name} is missing the MOCKUP ribbon"
        )


def test_every_page_has_the_lifecycle_nav_link():
    """LD1 option (b): exactly one new nav entry, "Audit Lifecycle" -> /lifecycle,
    marked active on lifecycle pages -- and the six existing nav links are
    still present and unchanged in label."""
    existing_labels = [
        "Start an Audit", "T&amp;E Showcase", "Audit Runs",
        "Skill Library", "Management Actions", "Platform Trace",
    ]
    for path in _html_files():
        text = path.read_text()
        assert 'plat-nav-active" href="S01_lifecycle_hub.html">Audit Lifecycle<' in text, (
            f"{path.name} is missing the active 'Audit Lifecycle' nav link"
        )
        for label in existing_labels:
            assert f">{label}<" in text, f"{path.name} is missing the existing nav link {label!r}"


def test_no_forbidden_real_person_or_org_name_beyond_the_shared_pattern():
    """LIFECYCLE_design.md §8.1: hand-authored corpus documents reference
    only "the Company" -- never a real organisation. A spot check for names
    PORTABILITY_PATTERN does not itself cover: the SKILL-001 synthetic ExCo
    roster (CLAUDE.md §11) should never appear in these generic lifecycle
    mockups, which use their own unrelated placeholder names."""
    offenders = []
    forbidden_extra = re.compile(r"\bExCo\b.*\b(Chen|Nakamura|Okonkwo|Johansson|Mendes|Kowalski|Thompson|Delacroix|van den Berg|Ramirez|Fitzgerald)\b")
    for path in _html_files():
        text = path.read_text()
        if forbidden_extra.search(text):
            offenders.append(path.name)
    assert not offenders, f"real SKILL-001 ExCo names found in mockups: {offenders}"


def test_index_lists_every_screen():
    index_html = (MOCKUP_DIR / "index.html").read_text()
    for files in EXPECTED_SCREENS.values():
        for f in files:
            assert f'href="{f}"' in index_html, f"index.html does not link to {f}"
