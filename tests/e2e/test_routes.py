"""Every route renders without a Dash error overlay or console/network error.

Covers feature ids R1-R9 (see features.py).
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import goto, has_error_overlay

ROUTES = [
    ("R1", "/"),
    ("R2", "/workspace/tne"),
    ("R3", "/runs"),
    ("R4", "/skills"),
    ("R5", "/skills/SKILL-001"),
    ("R7", "/actions"),
    ("R8", "/trace"),
]


@pytest.mark.parametrize("feature_id,path", ROUTES, ids=[r[0] for r in ROUTES])
def test_route_renders_cleanly(watched_page, app_base_url, feature_id, path):
    page, watcher = watched_page
    goto(page, app_base_url, path)
    assert page.locator("#page-content").count() == 1
    assert not has_error_overlay(page)
    watcher.assert_clean()


@pytest.mark.parametrize("skill_id", ["SKILL-002", "SKILL-003", "SKILL-004", "SKILL-005"])
def test_stub_skill_methodology_renders_cleanly(watched_page, app_base_url, skill_id):
    """R6: non-T&E skills get a stub methodology page, not an error."""
    page, watcher = watched_page
    goto(page, app_base_url, f"/skills/{skill_id}")
    assert not has_error_overlay(page)
    watcher.assert_clean()
    assert page.get_by_text("Methodology under development").count() == 1


def test_unknown_route_falls_back_to_landing_page(watched_page, app_base_url):
    """R9: record actual behaviour of an unmatched pathname rather than
    assume one. route_page()'s default branch returns landing_page()."""
    page, watcher = watched_page
    goto(page, app_base_url, "/this/path/does/not/exist")
    assert not has_error_overlay(page)
    watcher.assert_clean()
    assert page.get_by_text("Start an audit analysis").count() >= 1
