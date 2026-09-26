"""P7 review workflow, real browser (docs/specs/P7_mapping_authoring_design.md
§3.10 "Browser" test row): three identities (identity_pages, conftest.py),
each its own browser context, drive prepare -> note raised -> responded ->
cleared -> reviewed -> signed off -> XLSX download against
running_app["run_ids"]["review_workflow"] (conftest.py) -- a run never
touched by any other e2e_local test. A second test proves state survives an
App restart.

Scope note (this WP's final report): "the preparer trying to review sees
the SoD message" is interpreted here as the UI-R6 wrong-role refusal (the
preparer identity holds no reviewer role at all) -- the narrower literal
segregation-of-duties case (the SAME actor holding two roles on one run) is
already covered by tests/test_review_workflow.py's
test_sod_enforced_refuses_second_role_for_same_actor, a real threading
race included; reproducing it here would need a fourth, differently-
configured identity and was judged not worth the added fixture complexity
for a browser-level test whose job is proving the click path works."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

# A local copy, not `from conftest import ...` -- see test_runs_ui.py's own
# module docstring for why: app/tests/conftest.py and this package's
# conftest.py both load under the bare module name `conftest` (neither
# app/tests/ nor app/tests/e2e_local/ has an __init__.py), so importing one
# by that name collides when this file runs in the same session as
# app/tests/test_runs_page.py.
REPO_ROOT = Path(__file__).resolve().parents[3]


def goto(page, base_url: str, path: str, wait_until: str = "networkidle"):
    page.goto(base_url + path, wait_until=wait_until)
    return page


def _poll_until_reload(page, base_url: str, path: str, condition, *, timeout_s: float = 60.0, interval_s: float = 2.0) -> None:
    """Re-navigates to `path` every `interval_s` until `condition()` is true
    -- see the "Approver: sign off" comment above for why this test does
    not simply trust the page's own dcc.Interval poll here.

    Root-cause fix (2026-09-26, e2e_local merge-regression investigation):
    this used `wait_until="load"`, not this module's own `goto()` default of
    "networkidle". "load" fires once the initial HTML document and its
    directly-referenced scripts/styles have loaded -- it does NOT wait for
    Dash's own client-side XHR to `_dash-update-component` that actually
    populates `run-page-body` (the `run-poll` Interval's n_intervals=0
    callback, fired by Dash's JS after "load"). `condition()` itself
    (`page.locator(...).count()`) is a non-waiting snapshot, so every
    iteration raced that XHR: check the DOM the instant "load" fires, before
    the callback response has landed and re-rendered. That race was later
    only exposed, not created, by this workpaper -- the P7 review page
    renders three extra panels (narration review, AI-proposed findings,
    review notes), each an extra read/serialisation the callback must do
    before responding, pushing it reliably past the "load" event where the
    lighter pre-P7 page's callback often wasn't. The result: every one of
    the 150 reload iterations in the 300s budget sampled too early, and the
    loop timed out even though the run had reached `completed` within
    ~10s of sign-off (confirmed against the run's own persisted RunState).
    "networkidle" waits for the page's in-flight network requests --
    including that XHR -- to settle before returning, so the check that
    follows sees the callback's actual response."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if condition():
            return
        time.sleep(interval_s)
        goto(page, base_url, path, wait_until="networkidle")
    if not condition():
        raise AssertionError(f"condition not met within {timeout_s}s polling {path}")


def _visible_soon(locator, *, timeout_ms: float = 3000.0):
    """BUG-FLAKY-REVIEW-UI-1 (independent review round 5): `_poll_until_
    reload`'s own `condition()` is typically `page.locator(...).count() > 0`
    -- an instant, non-waiting DOM snapshot taken the moment `goto(...,
    wait_until="networkidle")` returns. "networkidle" guarantees the
    network settled, not that React has finished committing the DOM update
    the settled response produced, and a Dash callback chain can leave a
    brief further gap (a second, quieter round trip after the first burst)
    that "networkidle" alone does not span. That gap is the observed 1-in-5
    flake: the run had reached `completed` seconds earlier, but this
    particular reload's snapshot landed inside the gap. Wrapping the same
    locator in Playwright's own bounded, polling `wait_for` (never a fixed
    sleep) absorbs exactly that gap without weakening the check -- it still
    fails, and `_poll_until_reload` still raises after its own `timeout_s`,
    if the element genuinely never appears."""
    try:
        locator.first.wait_for(state="visible", timeout=timeout_ms)
        return True
    except PlaywrightTimeoutError:
        return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_http_200(url: str, timeout: float) -> None:
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last_err = e
        time.sleep(0.2)
    raise RuntimeError(f"App did not respond 200 at {url} within {timeout}s (last error: {last_err})")


def _strip_workspace_env(env: dict) -> dict:
    for k in (
        "MODEL_SONNET", "MODEL_GPT_OSS", "MODEL_ENDPOINT_HOST",
        "DATABRICKS_HOST", "DATABRICKS_TOKEN",
        "DBX_CATALOG", "DBX_SCHEMA", "DBX_VOLUME", "DBX_WAREHOUSE_HTTP_PATH",
        "DBX_SOURCE_SCHEMAS", "DBX_APP_NAME",
    ):
        env.pop(k, None)
    return env


def _drain_output(proc: subprocess.Popen, maxlen: int = 4000) -> deque:
    """See conftest.py's own `_drain_output` -- an unread `subprocess.PIPE`
    fills its OS pipe buffer once Werkzeug's per-request logging accumulates
    enough lines, and the child then blocks on its next write, wedging the
    whole app (every thread that also tries to log blocks on the same
    handler lock). Draining continuously prevents that."""
    lines: deque[str] = deque(maxlen=maxlen)

    def _pump() -> None:
        try:
            for line in proc.stdout:
                lines.append(line)
        except (ValueError, OSError):
            pass

    threading.Thread(target=_pump, daemon=True, name="e2e-local-log-drain").start()
    return lines


def _launch_app_subprocess(base_env: dict) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    full_env = dict(os.environ)
    _strip_workspace_env(full_env)
    full_env.update(base_env)
    full_env["PORT"] = str(port)
    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "app" / "app.py")],
        cwd=str(REPO_ROOT), env=full_env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    log_lines = _drain_output(proc)
    proc.e2e_log_lines = log_lines
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_http_200(base_url + "/ready", timeout=30.0)
    except Exception:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        raise RuntimeError(f"app/app.py failed to start on {base_url}:\n{''.join(log_lines)}")
    return proc, base_url


def _stop_app_subprocess(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def test_full_review_workflow_across_three_identities(running_app, identity_pages):
    base_url = running_app["base_url"]
    run_id = running_app["run_ids"]["review_workflow"]
    preparer = identity_pages["preparer"]
    reviewer = identity_pages["reviewer"]
    approver = identity_pages["approver"]

    # ── Preparer: mark as prepared ───────────────────────────────────────
    goto(preparer, base_url, f"/run/{run_id}")
    preparer.wait_for_selector("#run-mark-prepared-btn")
    preparer.locator("#run-mark-prepared-btn").click()
    preparer.wait_for_selector("text=awaiting review")
    assert "Prepared by e2e-preparer@example.invalid" in preparer.content()

    # ── Reviewer: raise a note ───────────────────────────────────────────
    goto(reviewer, base_url, f"/run/{run_id}")
    reviewer.wait_for_selector("#review-note-raise-btn")
    reviewer.locator("#review-note-raise-body").fill("justify excluding this")
    reviewer.locator("#review-note-raise-btn").click()
    reviewer.wait_for_selector("text=justify excluding this")

    # An open note blocks "Mark as reviewed" (UI-R6).
    reviewer.locator("#run-mark-reviewed-btn").click()
    reviewer.wait_for_selector("text=Action blocked")
    assert "open" in reviewer.content()

    # ── Preparer: respond to the note ────────────────────────────────────
    goto(preparer, base_url, f"/run/{run_id}")
    preparer.wait_for_selector("text=justify excluding this")
    preparer.locator('[id*="review-note-response-input"]').first.fill("because X")
    preparer.locator('[id*="review-note-respond-btn"]').first.click()
    preparer.wait_for_selector("text=Response by e2e-preparer@example.invalid: because X")

    # The preparer is refused as a reviewer -- UI-R6's own wrong-role text.
    preparer.locator("#run-mark-reviewed-btn").click()
    preparer.wait_for_selector("text=Action blocked")
    assert "not in a preparer/reviewer/approver group" in preparer.content()

    # ── Reviewer: clear the note, then mark as reviewed ──────────────────
    goto(reviewer, base_url, f"/run/{run_id}")
    reviewer.wait_for_selector('[id*="review-note-clear-btn"]')
    reviewer.locator('[id*="review-note-clear-btn"]').first.click()
    reviewer.wait_for_selector("text=Cleared")
    reviewer.locator("#run-mark-reviewed-btn").click()
    reviewer.wait_for_selector("#run-signoff-open-btn")
    assert "Reviewed by e2e-reviewer@example.invalid" in reviewer.content()

    # ── Approver: sign off (native confirm dialog) ───────────────────────
    goto(approver, base_url, f"/run/{run_id}")
    approver.on("dialog", lambda d: d.accept())
    approver.wait_for_selector("#run-signoff-open-btn")
    approver.locator("#run-signoff-open-btn").click()
    # The click itself only opens/confirms the dialog and calls sign_off
    # synchronously (phase -> 'export', status -> 'queued'); the export NODE
    # then runs asynchronously through the real ThreadExecutor. Polls via
    # page.goto() reload rather than trusting the page's own 3s dcc.Interval,
    # matching tests/e2e/test_connected_app.py's own established pattern for
    # the same wait. The condition wraps the locator in `_visible_soon`
    # (Playwright's own bounded `wait_for`, BUG-FLAKY-REVIEW-UI-1) rather
    # than an instant `.count() > 0` snapshot -- `page.content()` has the
    # same "read before React catches up" race `_visible_soon` avoids, and
    # a bare `.count()` right after "networkidle" still had a narrower
    # version of exactly that race, observed live as a 1-in-5 flake.
    # A generous timeout (matching tests/e2e/test_connected_app.py's own
    # _EXPORT_TIMEOUT_S=300 for the same wait): this sandbox's single-CPU
    # constraint (CLAUDE.md §2.5) makes real pipeline execution alongside
    # several concurrent browser contexts noticeably variable run to run.
    _poll_until_reload(
        approver, base_url, f"/run/{run_id}", lambda: _visible_soon(approver.locator("text=Run complete")),
        timeout_s=300.0,
    )
    text = approver.content()
    assert "Prepared by e2e-preparer@example.invalid" in text
    assert "reviewed by e2e-reviewer@example.invalid" in text
    assert "signed off by e2e-approver@example.invalid" in text
    assert "Self-approved" not in text

    # XLSX still downloads for a P7-signed-off run.
    with approver.expect_download() as dl_info:
        approver.locator("#run-download-xlsx-btn").click()
    download = dl_info.value
    assert download.suggested_filename.endswith(".xlsx")

    # State survives a reload (Delta/SQLite round trip, not callback memory).
    approver.reload(wait_until="load")
    approver.wait_for_selector("text=Run complete")
    assert "signed off by e2e-approver@example.invalid" in approver.content()


def test_review_stage_survives_an_app_restart(running_app, identity_pages, browser):
    """§3.10: a run paused mid-review (stage 'review', one open note)
    survives the App container restarting -- the same guarantee CLAUDE.md
    §2.3 rule 2 gives every other RunState field, exercised here for
    RunState.review specifically. Uses running_app's OWN db_path/env so the
    restarted process attaches to the exact same persisted state; the
    restart itself is this test's own subprocess, not running_app's shared
    one (every other e2e_local test keeps running against that)."""
    base_url = running_app["base_url"]
    run_id = running_app["run_ids"]["review_workflow"]
    preparer = identity_pages["preparer"]
    reviewer = identity_pages["reviewer"]

    # This run was already carried through prepare/note/respond/clear/review/
    # sign-off by the previous test in this module (pytest runs a module's
    # tests in file order by default) -- reaching into a NEW run instead
    # keeps this test independent of that ordering assumption.
    from orchestrator import service

    ctx = service.build_app_context(dict(running_app["env"]))
    fresh_run_id = service.start_audit_run(
        ctx, skill_id="SKILL-MINI",
        bindings={k: v for k, v in service.suggest_bindings(ctx, "SKILL-MINI").items()},
        audit_period=("2026-01-01", "2026-01-31"),
        objective="e2e_local: P7 restart resilience", run_owner="tester",
    )
    import time
    deadline = time.time() + 30
    while time.time() < deadline:
        if service.get_run(ctx, fresh_run_id)["status"] in ("awaiting_signoff", "failed"):
            break
        time.sleep(0.1)
    assert service.get_run(ctx, fresh_run_id)["status"] == "awaiting_signoff"
    service.prepare_findings(ctx, fresh_run_id, "e2e-preparer@example.invalid")

    goto(reviewer, base_url, f"/run/{fresh_run_id}")
    reviewer.wait_for_selector("#review-note-raise-btn")
    reviewer.locator("#review-note-raise-body").fill("survive a restart")
    reviewer.locator("#review-note-raise-btn").click()
    reviewer.wait_for_selector("text=survive a restart")

    # Restart: a second app.py process attached to the SAME db_path.
    proc, restarted_base_url = _launch_app_subprocess(dict(running_app["env"]))
    try:
        goto(reviewer, restarted_base_url, f"/run/{fresh_run_id}")
        reviewer.wait_for_selector("text=awaiting review")
        text = reviewer.content()
        assert "survive a restart" in text
        assert "Prepared by e2e-preparer@example.invalid" in text
    except Exception:
        # The guard for this restarted process's OWN subprocess (conftest.py's
        # `_dump_server_log_on_failure` only covers `running_app`'s process,
        # not this test's second one) -- print its drained log tail so a hang
        # or crash here is diagnosable too, not just a bare timeout.
        tail = "".join(list(proc.e2e_log_lines)[-200:])
        print(f"\n----- restarted app subprocess log tail -----\n{tail}----- end -----\n")
        raise
    finally:
        _stop_app_subprocess(proc)
