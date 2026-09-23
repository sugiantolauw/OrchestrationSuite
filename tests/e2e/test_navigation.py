"""Platform nav strip: every link is present and reaches its route.

Covers feature id N1.
"""

from __future__ import annotations

from tests.e2e.conftest import goto, has_error_overlay

NAV_LINKS = [
    ("Start an Audit", "/"),
    ("T&E Showcase", "/workspace/tne"),
    ("Audit Runs", "/runs"),
    ("Skill Library", "/skills"),
    ("Management Actions", "/actions"),
    ("Platform Trace", "/trace"),
]


def test_nav_links_present_and_navigate(watched_page, app_base_url):
    page, watcher = watched_page
    goto(page, app_base_url, "/")

    for label, path in NAV_LINKS:
        link = page.locator(f'nav a:has-text("{label}")')
        assert link.count() == 1, f"nav link '{label}' not found"
        assert link.get_attribute("href") == path

    for label, path in NAV_LINKS:
        page.locator(f'nav a:has-text("{label}")').click()
        page.wait_for_load_state("networkidle")
        assert path in page.url, f"clicking '{label}' did not reach {path} (at {page.url})"
        assert not has_error_overlay(page)

    watcher.assert_clean()
