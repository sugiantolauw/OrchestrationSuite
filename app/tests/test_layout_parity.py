"""Layout parity against the prototype (reference_app/src/platform/pages.py
+ components.py): for each page, the set of element ids and the ordered
tree of (tag, id, className) must match the prototype's rendering of the
SAME data, exactly. No allow-list: every page covered here must diverge in
zero elements. See the change report for why /workspace/tne is not (yet)
covered by this test, and why /run/<id> is not covered at all (the
prototype has no equivalent page).

This is a regression lock: future changes to app/'s ported pages that add,
remove, relabel, disable or restyle an element will fail this test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_APP_DIR = os.path.dirname(_TESTS_DIR)
_REPO_ROOT = os.path.dirname(_APP_DIR)

sys.path.insert(0, _TESTS_DIR)
from tree import shape, render  # noqa: E402


def _reference_tree(page: str) -> str:
    result = subprocess.run(
        [sys.executable, os.path.join(_TESTS_DIR, "reference_tree.py"), _REPO_ROOT, page],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.rstrip("\n")


@pytest.fixture(scope="module")
def reference_fixtures():
    result = subprocess.run(
        [sys.executable, os.path.join(_TESTS_DIR, "dump_reference_fixtures.py"), _REPO_ROOT],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def _app_tree(component) -> str:
    return render(shape(component))


def test_landing_page_matches_prototype(monkeypatch, reference_fixtures):
    from src.platform import adapters
    from src.run_setup import home_layout

    assets = reference_fixtures["DEMO_DATA_ASSETS"]
    monkeypatch.setattr(adapters, "list_skills", lambda filters=None: reference_fixtures["DEMO_SKILLS"])
    monkeypatch.setattr(
        adapters, "search_governed_data",
        lambda q: assets if not q else [a for a in assets if q.lower() in a["name"].lower()],
    )
    monkeypatch.setattr(adapters, "is_demo_mode", lambda: True)
    # Any string works here: the tree comparison is structural (tag/id/
    # className) only, never text content, so this need not (and must not,
    # tests/test_portability.py NN16) be the prototype's own hardcoded path.
    monkeypatch.setattr(adapters, "get_upload_base_path", lambda: "/Volumes/placeholder/uploads")

    assert _app_tree(home_layout()) == _reference_tree("landing")


def test_skill_library_page_matches_prototype(monkeypatch, reference_fixtures):
    from src.platform import adapters
    from src.platform.pages import skill_library_page

    monkeypatch.setattr(adapters, "list_skills", lambda filters=None: reference_fixtures["DEMO_SKILLS"])
    monkeypatch.setattr(adapters, "is_demo_mode", lambda: True)

    assert _app_tree(skill_library_page()) == _reference_tree("skills")


def test_audit_runs_page_matches_prototype(monkeypatch, reference_fixtures):
    from src.platform import adapters
    from src.platform.pages import audit_runs_page

    monkeypatch.setattr(adapters, "list_audit_runs", lambda filters=None: reference_fixtures["DEMO_AUDIT_RUNS"])
    monkeypatch.setattr(adapters, "is_demo_mode", lambda: True)

    assert _app_tree(audit_runs_page()) == _reference_tree("runs")


def test_platform_trace_page_matches_prototype(monkeypatch, reference_fixtures):
    from src.platform import adapters
    from src.platform.pages import platform_trace_page

    monkeypatch.setattr(adapters, "list_audit_runs", lambda filters=None: reference_fixtures["DEMO_AUDIT_RUNS"])
    monkeypatch.setattr(adapters, "list_trace_events", lambda run_id=None: reference_fixtures["DEMO_TRACE_EVENTS"])
    monkeypatch.setattr(adapters, "is_demo_mode", lambda: True)

    assert _app_tree(platform_trace_page()) == _reference_tree("trace")


def test_management_actions_page_matches_prototype(monkeypatch, reference_fixtures):
    from src.platform import adapters
    from src.platform.pages import management_actions_page

    monkeypatch.setattr(
        adapters, "list_management_actions", lambda filters=None: reference_fixtures["DEMO_MANAGEMENT_ACTIONS"]
    )
    monkeypatch.setattr(adapters, "is_demo_mode", lambda: True)

    assert _app_tree(management_actions_page()) == _reference_tree("actions")
