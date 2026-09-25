"""Real-browser tests against app/app.py + a real local backend (see
conftest.py's `running_app`). These exist because BUG-RUNVIEW-1 (the /runs
"View" button doing nothing in a real browser) and the management-action
edit not surviving a real page reload both passed every unit test that
calls a Dash callback function directly -- neither defect is visible
without an actual browser driving an actual running server."""

from __future__ import annotations

import time

# A local helper, not `from conftest import goto` -- app/tests/conftest.py
# (sibling package, autouse fixtures for the fake-service unit suite) and
# this package's own conftest.py both load under the bare module name
# `conftest` (neither app/tests/ nor app/tests/e2e_local/ has an
# __init__.py), so importing one by that name here and running this file
# in the same pytest session as app/tests/test_runs_page.py (which does
# `from conftest import load_app_entry`) makes Python's sys.modules cache
# resolve "conftest" to whichever loaded first -- a real collision, not a
# hypothetical one (reproduced running both together during this task).


def goto(page, base_url: str, path: str, wait_until: str = "networkidle"):
    page.goto(base_url + path, wait_until=wait_until)
    return page


def _select_option(page, dropdown_id: str, option_text: str):
    """See tests/e2e/test_workspace_tne.py's own `_select_react_select_option`
    -- same dash-bootstrap-components dcc.Dropdown markup, so the same
    virtualized-menu interaction applies here."""
    page.locator(f"#{dropdown_id}").click()
    menu = page.locator(f"#{dropdown_id} .Select-menu-outer")
    menu.wait_for(state="visible")
    menu.locator(".VirtualizedSelectOption", has_text=option_text).first.click()


def _run_card(page, run_id: str):
    return page.locator(".run-card", has_text=run_id)


# ── BUG-RUNVIEW-1 root cause: a gratuitous initial re-render ────────────────

def test_runs_list_is_not_rerendered_on_page_load(running_app, page):
    """The actual root cause: `_filter_runs` (src/runs_page.py) used to have
    no `prevent_initial_call`, so Dash fired it once on every /runs mount
    even though both dcc.Dropdown filters start at their default (no
    filter) value -- replacing the whole server-rendered runs-list.children
    subtree (and every run-view-btn/run-export-btn/run-trace-btn pattern-
    matching button inside it) with a second, freshly-mounted render right
    after paint. That is the same "subtree replaced out from under a click
    shortly after mount" shape as the sign-off race
    tests/e2e/test_connected_app.py's module docstring documents fixing
    once already -- a click landing in that window updates a button
    instance about to be superseded. This asserts the fix directly and
    deterministically (no timing race to win or lose): no
    `_dash-update-component` request whose output is `runs-list.children`
    happens between page load and any user interaction."""
    base_url = running_app["base_url"]
    dash_requests: list[str] = []

    def _on_request(req):
        if "_dash-update-component" in req.url:
            body = req.post_data or ""
            if "runs-list.children" in body:
                dash_requests.append(body)

    page.on("request", _on_request)
    goto(page, base_url, "/runs")
    assert not dash_requests, (
        "runs-list.children was re-rendered on page load with no user "
        f"interaction: {dash_requests}"
    )


# ── View routes to the right page for the run's shape ───────────────────────

def test_view_completed_workspace_run_lands_on_workspace_tne(running_app, watched_page):
    """CLAUDE.md §11 'Run cards on /runs': a completed run of a Skill with a
    dedicated workspace page opens /workspace/tne?run_id=<id>."""
    page, watcher = watched_page
    base_url = running_app["base_url"]
    run_id = running_app["run_ids"]["workspace_completed"]
    goto(page, base_url, "/runs")
    _run_card(page, run_id).get_by_text("View", exact=True).click()
    page.wait_for_url(f"**/workspace/tne?run_id={run_id}", timeout=10_000)
    watcher.assert_clean()


def test_view_non_workspace_completed_run_lands_on_run_page(running_app, watched_page):
    """A completed run of a Skill with no workspace page (SKILL-MINI has no
    workspace.py) opens /run/<id> -- the one page every run kind and
    status already renders."""
    page, watcher = watched_page
    base_url = running_app["base_url"]
    run_id = running_app["run_ids"]["completed"]
    goto(page, base_url, "/runs")
    _run_card(page, run_id).get_by_text("View", exact=True).click()
    page.wait_for_url(f"**/run/{run_id}", timeout=10_000)
    watcher.assert_clean()


def test_view_awaiting_signoff_non_workspace_run_lands_on_run_page(running_app, watched_page):
    page, watcher = watched_page
    base_url = running_app["base_url"]
    run_id = running_app["run_ids"]["awaiting_signoff"]
    goto(page, base_url, "/runs")
    _run_card(page, run_id).get_by_text("View", exact=True).click()
    page.wait_for_url(f"**/run/{run_id}", timeout=10_000)
    watcher.assert_clean()


def test_view_works_for_every_run_card_on_the_page_not_just_the_first(running_app, page):
    """BUG-RUNVIEW-1 was reported across several different run_ids on the
    same page -- click every run card's View button in turn (not just the
    first) and confirm each one actually navigates."""
    base_url = running_app["base_url"]
    run_ids = running_app["run_ids"]
    targets = [run_ids["completed"], run_ids["workspace_completed"],
               run_ids["awaiting_signoff"], run_ids["completed_2"]]
    for run_id in targets:
        goto(page, base_url, "/runs")
        _run_card(page, run_id).get_by_text("View", exact=True).click()
        page.wait_for_url(lambda url, rid=run_id: rid in url, timeout=10_000)
        assert run_id in page.url


# ── Export ───────────────────────────────────────────────────────────────

def test_export_downloads_xlsx_for_a_signed_off_run(running_app, page):
    base_url = running_app["base_url"]
    run_id = running_app["run_ids"]["completed"]
    goto(page, base_url, "/runs")
    with page.expect_download(timeout=15_000) as dl_info:
        _run_card(page, run_id).get_by_text("Export", exact=True).click()
    download = dl_info.value
    assert download.suggested_filename.endswith(".xlsx")


def test_export_shows_presignoff_message_before_signoff(running_app, page):
    """CLAUDE.md §11 'Message line': before sign-off no xlsx export is
    recorded yet -- Export states that plainly instead of no-opping."""
    base_url = running_app["base_url"]
    run_id = running_app["run_ids"]["awaiting_signoff"]
    goto(page, base_url, "/runs")
    _run_card(page, run_id).get_by_text("Export", exact=True).click()
    page.wait_for_selector("text=This run must be signed off before its export is available.", timeout=10_000)


# ── Trace ────────────────────────────────────────────────────────────────

def test_trace_lands_on_trace_with_run_preselected(running_app, watched_page):
    page, watcher = watched_page
    base_url = running_app["base_url"]
    run_id = running_app["run_ids"]["completed"]
    goto(page, base_url, "/runs")
    _run_card(page, run_id).get_by_text("Trace", exact=True).click()
    page.wait_for_url(f"**/trace?run_id={run_id}", timeout=10_000)

    filtered_rows = page.locator("#trace-events-body tr").count()
    # Clearing the filter (an explicit, deliberate value change -- not the
    # gratuitous initial re-render this suite regresses against above)
    # must show at least as many rows as the preselected, narrowed view.
    page.locator("#trace-run-filter .Select-clear").click()
    page.wait_for_timeout(500)
    all_rows = page.locator("#trace-events-body tr").count()
    assert all_rows >= filtered_rows
    watcher.assert_clean()


# ── Filters narrow the list (BUG-RUNS-1 / ACTIONS-1 / SKILLS-1 / TRACE-1
#    already fixed the wiring; this proves it still works) ────────────────

def test_runs_page_status_filter_narrows_the_list(running_app, page):
    base_url = running_app["base_url"]
    goto(page, base_url, "/runs")
    total = page.locator(".run-card").count()
    assert total >= 4

    _select_option(page, "runs-status-filter", "Completed")
    page.wait_for_timeout(500)
    completed_count = page.locator(".run-card").count()
    assert 0 < completed_count < total, (
        f"status=Completed should narrow the {total} cards, got {completed_count}"
    )

    _select_option(page, "runs-status-filter", "Running")
    page.wait_for_timeout(500)
    assert page.locator(".run-card").count() == 0


def test_actions_page_status_filter_narrows_the_list(running_app, page):
    base_url = running_app["base_url"]
    goto(page, base_url, "/actions")
    total = page.locator("#actions-table-body tr").count()
    assert total > 0

    _select_option(page, "actions-status-filter", "Closed")
    page.wait_for_timeout(500)
    assert page.locator("#actions-table-body tr").count() == 0

    _select_option(page, "actions-status-filter", "Open")
    page.wait_for_timeout(500)
    # Clearing back to "no filter" must restore the full, unfiltered set.
    page.locator("#actions-status-filter .Select-clear").click()
    page.wait_for_timeout(500)
    assert page.locator("#actions-table-body tr").count() == total


def test_skills_page_search_narrows_the_list(running_app, page):
    base_url = running_app["base_url"]
    goto(page, base_url, "/skills")
    total = page.locator(".skill-card").count()
    assert total >= 2

    search = page.locator("#skill-search")
    search.fill("mini")
    search.press("Tab")  # dcc.Input(debounce=True) fires "value" on blur/Enter, not every keystroke
    page.wait_for_timeout(700)
    assert page.locator(".skill-card").count() == total

    search.fill("zzz-no-such-skill")
    search.press("Tab")
    page.wait_for_timeout(700)
    assert page.locator(".skill-card").count() == 0


def test_trace_page_run_filter_narrows_the_list(running_app, page):
    base_url = running_app["base_url"]
    run_id = running_app["run_ids"]["completed"]
    goto(page, base_url, "/trace")
    total = page.locator("#trace-events-body tr").count()
    assert total > 0

    _select_option(page, "trace-run-filter", run_id)
    page.wait_for_timeout(500)
    narrowed = page.locator("#trace-events-body tr").count()
    assert 0 < narrowed <= total


# ── Management action edit survives a real reload ───────────────────────

def test_editing_a_management_action_persists_across_reload(running_app, watched_page):
    page, watcher = watched_page
    base_url = running_app["base_url"]
    run_id = running_app["run_ids"]["completed"]

    goto(page, base_url, f"/workspace/tne?run_id={run_id}")
    page.get_by_role("tab", name="Findings & Actions").click()
    page.get_by_role("tab", name="Management Actions").click()
    page.wait_for_selector("#tne-mgmt-tracker-body tr")

    page.locator("#tne-mgmt-tracker-body button", has_text="Edit").first.click()
    page.locator("#tne-action-owner").wait_for(state="visible", timeout=10_000)

    new_owner = f"E2E Owner {int(time.time())}"
    owner_input = page.locator("#tne-action-owner")
    owner_input.fill("")
    owner_input.fill(new_owner)
    _select_option(page, "tne-action-status", "Agreed")
    page.locator("#tne-action-save").click()
    page.wait_for_timeout(800)

    page.reload(wait_until="networkidle")
    page.get_by_role("tab", name="Findings & Actions").click()
    page.get_by_role("tab", name="Management Actions").click()
    page.wait_for_selector("#tne-mgmt-tracker-body tr")

    assert page.get_by_text(new_owner).count() >= 1, (
        f"edited owner {new_owner!r} did not survive a page reload"
    )
    assert page.get_by_text("Agreed").count() >= 1
    watcher.assert_clean()


# ── Start audit analysis navigates quickly ──────────────────────────────

def test_start_audit_analysis_navigates_to_run_page_quickly(running_app, watched_page):
    """CLAUDE.md §11 'Run start opens the run page at once': the button
    navigates within about a second, well before the run's own multi-write
    creation (submitted to pending_runs' background worker) completes."""
    page, watcher = watched_page
    base_url = running_app["base_url"]
    goto(page, base_url, "/")

    page.locator('[id*="skill-select-card"][id*="SKILL-MINI-WS"]').first.click()
    page.wait_for_timeout(300)

    started_at = time.monotonic()
    page.locator("#start-run-btn").click()
    page.wait_for_url("**/run/RUN-*", timeout=10_000)
    elapsed = time.monotonic() - started_at

    assert elapsed < 5.0, f"navigation to /run/<id> took {elapsed:.2f}s, expected roughly ~1s"
    watcher.assert_clean()


# ── 4.1 (final round 4A): the auto-confirm checkbox ─────────────────────────

def test_unchecking_show_proposed_approach_auto_confirms_the_plan(running_app, watched_page):
    """Round-4A live test 4.1 found the Playwright uncheck() on 'Show
    proposed approach before execution' reporting 'did not change its
    state', and could not tell whether that was a product defect in the
    checkbox/auto_confirm_plan wiring or a Playwright artifact. The wiring
    itself (src/run_setup.py start_run: review_plan_first="preview_plan"
    in options -> service.py's auto_confirm_plan=not review_plan_first) is
    plain, uncontrolled client-side dcc.Checklist state with no Dash Output
    ever writing back to it -- nothing should fight a real click. This
    drives the actual checkbox in a real browser and asserts the run
    reaches awaiting_signoff directly, WITHOUT ever needing a manual
    'Confirm plan' click -- the actual product behaviour the checkbox is
    supposed to control, not just its raw checked/unchecked DOM state."""
    page, watcher = watched_page
    base_url = running_app["base_url"]
    goto(page, base_url, "/")

    page.locator('[id*="skill-select-card"][id*="SKILL-MINI-WS"]').first.click()
    page.wait_for_timeout(300)

    # "Advanced parameters" is a collapsed <details>; the checkbox is not
    # interactable until it is expanded.
    page.locator("summary", has_text="Advanced parameters").click()
    checkbox = page.locator("#audit-options input[type=checkbox]").first
    checkbox.wait_for(state="visible")
    assert checkbox.is_checked(), "expected 'Show proposed approach' checked by default"
    checkbox.uncheck()
    assert not checkbox.is_checked(), "unchecking the checkbox did not change its DOM state"

    page.locator("#start-run-btn").click()
    page.wait_for_url("**/run/RUN-*", timeout=10_000)

    # The run must reach awaiting_signoff directly -- if auto_confirm_plan
    # did not take effect, it would stop at awaiting_confirmation and show
    # "Confirm plan" instead (CLAUDE.md §2.4: playbook auto-confirm skips
    # the confirmation gate entirely, never pausing there even briefly).
    gate = page.locator("#run-signoff-open-btn, #run-confirm-plan-btn").first
    gate.wait_for(state="visible", timeout=20_000)
    assert page.locator("#run-confirm-plan-btn").count() == 0, (
        "run stopped at awaiting_confirmation -- auto_confirm_plan did not take effect"
    )
    assert page.locator("#run-signoff-open-btn").count() == 1
    watcher.assert_clean()
