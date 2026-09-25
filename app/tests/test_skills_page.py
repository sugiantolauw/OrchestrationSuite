"""src/skills_page.py — BUG-SKILLS-1's fix (mirroring tests/test_runs_page.py
and tests/test_trace_page.py exactly): the /skills page's "Search skills…"
input and its Domain/Status dropdowns must actually filter the skill card
list. Rendered against the REAL orchestrator.service + LocalPersistence
(overriding this directory's autouse fake_backend fixture), reading the
repo's own skills/ directory (skills/tne_exco, id SKILL-001) plus one
fixture skill with a different domain and status symlinked in alongside
it, so the domain/status filters have two genuinely distinct values to
narrow between."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import load_app_entry
from src import skills_page
from src.platform import adapters

_REPO_ROOT = Path(__file__).resolve().parents[2]

_FIXTURE_SKILL_MANIFEST = """\
id: SKILL-FIXTURE-AP
name: Fixture AP Duplicate Check
domain: Accounts Payable
version: "0.1.0"
owner: Test Owner
status: published
description: >
  A fixture skill used only by tests/test_skills_page.py to exercise the
  /skills domain and status filters against a value genuinely different
  from the repo's real Travel & Expenses / Draft skill.
"""


@pytest.fixture
def real_ctx(monkeypatch, tmp_path):
    from orchestrator import service as real_service

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    # Symlink (not copy) the repo's real skills/tne_exco in alongside the
    # fixture skill below, so this test exercises the filters against a
    # genuine SKILL-001 (the same one every other real-backend test in this
    # directory points SKILLS_DIR at) plus one skill with a deliberately
    # different domain/status, rather than a hand-built stand-in for both.
    os.symlink(_REPO_ROOT / "skills" / "tne_exco", skills_dir / "tne_exco")
    fixture_dir = skills_dir / "fixture_ap"
    fixture_dir.mkdir()
    (fixture_dir / "manifest.yaml").write_text(_FIXTURE_SKILL_MANIFEST)

    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(tmp_path),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
        "SKILLS_DIR": str(skills_dir),
    }
    monkeypatch.setattr(adapters, "service", real_service)
    adapters._ctx = None
    original_build = real_service.build_app_context
    monkeypatch.setattr(real_service, "build_app_context", lambda *a, **k: original_build(env))

    ctx = adapters.get_context()
    try:
        yield ctx
    finally:
        ctx.executor.stop()
        adapters._ctx = None


def test_clearing_all_filters_shows_every_skill_default(real_ctx):
    rows = skills_page._rows_for_filter(None, None, None)
    text = str(rows)
    assert "SKILL-001" in text
    assert "SKILL-FIXTURE-AP" in text


def test_search_matches_skill_name_case_insensitively(real_ctx):
    rows = skills_page._rows_for_filter("executive diligence", None, None)
    text = str(rows)
    assert "SKILL-001" in text
    assert "SKILL-FIXTURE-AP" not in text


def test_search_matches_skill_id(real_ctx):
    rows = skills_page._rows_for_filter("skill-fixture-ap", None, None)
    text = str(rows)
    assert "SKILL-FIXTURE-AP" in text
    assert "SKILL-001" not in text


def test_search_matches_description(real_ctx):
    rows = skills_page._rows_for_filter("duplicate check", None, None)
    text = str(rows)
    assert "SKILL-FIXTURE-AP" in text
    assert "SKILL-001" not in text


def test_domain_filter_narrows_to_the_matching_domain(real_ctx):
    rows = skills_page._rows_for_filter(None, "Accounts Payable", None)
    text = str(rows)
    assert "SKILL-FIXTURE-AP" in text
    assert "SKILL-001" not in text


def test_status_filter_narrows_to_the_matching_status(real_ctx):
    rows = skills_page._rows_for_filter(None, None, "Published")
    text = str(rows)
    assert "SKILL-FIXTURE-AP" in text
    assert "SKILL-001" not in text


def test_status_filter_is_case_insensitive(real_ctx):
    rows = skills_page._rows_for_filter(None, None, "draft")
    text = str(rows)
    assert "SKILL-001" in text
    assert "SKILL-FIXTURE-AP" not in text


def test_combined_search_and_domain_filters_apply_together(real_ctx):
    rows = skills_page._rows_for_filter("fixture", "Accounts Payable", None)
    text = str(rows)
    assert "SKILL-FIXTURE-AP" in text
    assert "SKILL-001" not in text

    rows = skills_page._rows_for_filter("fixture", "Travel & Expenses", None)
    text = str(rows)
    assert "SKILL-FIXTURE-AP" not in text
    assert "SKILL-001" not in text


def test_empty_string_filter_values_behave_like_cleared(real_ctx):
    rows = skills_page._rows_for_filter("", "", "")
    text = str(rows)
    assert "SKILL-001" in text
    assert "SKILL-FIXTURE-AP" in text


def test_filtering_issues_exactly_one_call_not_one_per_skill(real_ctx, monkeypatch):
    """CLAUDE.md §2.3 rule 4: filtering must not turn one page render into
    one query per skill."""
    calls: list[int] = []
    original = adapters.list_skills

    def _tracked():
        calls.append(1)
        return original()

    monkeypatch.setattr(adapters, "list_skills", _tracked)

    skills_page._rows_for_filter("fixture", "Accounts Payable", "Published")
    assert len(calls) == 1


def test_callback_is_registered_on_the_real_app(real_ctx):
    entry = load_app_entry()
    assert "skill-library-grid.children" in entry.app.callback_map
