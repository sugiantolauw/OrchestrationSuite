"""Browser E2E for the connected App (app/), against a real local backend
(ORCH_BACKEND=local, orchestrator.service — not a mock). Drives the actual
user journey CLAUDE.md's build brief asks for: pick SKILL-001, bind every
source, set period + objective, run, confirm/sign off, open /workspace/tne.

Collection is gated the same way as the rest of tests/e2e (RUN_E2E=1, see
conftest.py's pytest_ignore_collect) so the default unit run is unaffected.

This talks to a real pipeline over real synthetic data, so it is slow by
E2E standards — see _PIPELINE_TIMEOUT_S. Nothing here is mocked: if
orchestrator.service is missing or broken, this fails for real reasons,
not a stub gap.

Originally marked xfail(strict=False) over a different defect: two live
observations showed the `export` node never gaining a node_attempts row
after sign-off, appearing to hang for 10-15 minutes. Root-caused since: it
was never orchestrator/'s `export` node at all. app/src/run_status.py's
sign-off control used to be a server-rendered confirm/cancel panel living
INSIDE run-page-body, the same subtree the page's 3s dcc.Interval poll
re-renders. dash-renderer does not guarantee callback response ordering
across separate in-flight requests, so a poll response for a request
dispatched a moment before "Sign off findings" was clicked could still land
(and silently revert the open panel to the bare button) after the
open-confirm response — before sign_off was ever called, well before the
export phase could even be admitted. Fixed by moving the confirmation to a
dcc.ConfirmDialog (a native browser popup living outside run-page-body, so
it cannot be raced by the poll at all): verified deterministically, repeatedly,
against a fast fixture Skill (no manual page.reload() needed at all -- the
dialog opens and submits inside the page's own natural render cycle), and
via a new orchestrator-level regression test that drives plan -> execute ->
sign_off -> export -> completed through a real ThreadExecutor
(tests/test_p3_executor.py::test_full_run_reaches_completed_via_thread_executor).

The marker below is for a DIFFERENT, separately-observed issue: against the
real ~30MB synthetic_data/ xlsx (this test's own fixture), this sandbox's
single-process, single-CPU-constrained container cannot serve even a plain
page reload within 120s while profile/execute hold the GIL parsing that
file with openpyxl -- CLAUDE.md §2.5 names exactly this risk ("No
isolation... a heavy run degrades the UI for every user"). That is a real
performance characteristic of in-App execution under CPU contention, not a
callback-ordering bug, and is out of this pass's scope to fix (it would mean
moving Excel parsing off the request-serving thread's GIL budget, e.g.
chunked/async reads or a process pool). Re-run this test on a host with more
than one CPU core (or once source reads are less GIL-heavy) before relying
on it to gate a release; APP_URL against a real deployed App is the more
representative way to check this today.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.e2e.conftest import goto, has_error_overlay

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# The reference synthetic_data/ fixtures are tens of MB of .xlsx read with
# openpyxl; reaching `awaiting_signoff` (discover -> profile -> plan ->
# execute -> classify -> find -> prioritise -> act) has been observed to
# take ~4 minutes end to end.
_PIPELINE_TIMEOUT_S = 420
_EXPORT_TIMEOUT_S = 300


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
        time.sleep(0.5)
    raise RuntimeError(f"App did not respond 200 at {url} within {timeout}s (last error: {last_err})")


@pytest.fixture(scope="module")
def connected_app_url(tmp_path_factory):
    """Launches app/app.py for real, against a throwaway LocalPersistence
    db and the repo's synthetic_data/ fixtures — the same ORCH_BACKEND=local
    path a developer runs locally (README / CLAUDE.md build brief)."""
    external = os.environ.get("APP_URL")
    if external:
        # A real deployed App: REVIEW_* is whatever that deployment is
        # configured with (CLAUDE.md §11 "Development workspace: labelled
        # mode" for the dev workspace) -- this test does not set it.
        base = external.rstrip("/")
        _wait_for_http_200(base + "/", timeout=30.0)
        yield base
        return

    port = _free_port()
    tmp = tmp_path_factory.mktemp("orch")
    db_path = tmp / "orchestrator.db"
    # P7 review workflow (docs/specs/P7_mapping_authoring_design.md §3.5-3.6):
    # this test drives the whole prepare -> review -> sign-off sequence
    # through ONE browser identity (no X-Forwarded-Email header is set
    # anywhere in this file, so `_request_actor` falls back to the literal
    # "local-user" the local backend alone permits). The code default
    # (REVIEW_SOD_MODE=enforced, REVIEW_ROLE_SOURCE=workspace_groups) would
    # either refuse the second and third step (one actor cannot hold two
    # roles once enforced) or fail outright (no real workspace to resolve
    # SCIM groups against in this sandboxed subprocess) -- "config" +
    # "labelled" is exactly the development-workspace setting CLAUDE.md §11
    # records for this same situation, matching
    # app/tests/e2e_local/conftest.py's own `running_app` fixture.
    review_roles_path = tmp / "review_roles.yaml"
    review_roles_path.write_text("local-user: [preparer, reviewer, approver]\n")
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["ORCH_BACKEND"] = "local"
    env["ORCH_LOCAL_DB"] = str(db_path)
    env["ORCH_LOCAL_DATA_ROOT"] = str(REPO_ROOT / "synthetic_data")
    env["REVIEW_SOD_MODE"] = "labelled"
    env["REVIEW_ROLE_SOURCE"] = "config"
    env["REVIEW_ROLE_ASSIGNMENTS"] = str(review_roles_path)

    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "app" / "app.py")],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_http_200(base_url + "/ready", timeout=60.0)
    except Exception:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate(timeout=10)
        raise RuntimeError(f"app/app.py failed to start on {base_url}:\n{out}")

    yield base_url

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _poll_until(page, predicate, *, timeout_s: float, interval_ms: int = 3000, on_tick=None):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if on_tick is not None:
            on_tick()
        if predicate():
            return
        page.wait_for_timeout(interval_ms)
    raise AssertionError(f"Condition not met within {timeout_s}s")


@pytest.mark.xfail(
    strict=False,
    reason=(
        "page.reload() during profile/execute's real ~30MB xlsx parse "
        "exceeds even a 120s timeout in this single-CPU sandbox -- a GIL-"
        "contention performance characteristic (CLAUDE.md §2.5), not the "
        "sign-off race this test used to be xfailed for (see module "
        "docstring; that defect is fixed and independently verified)."
    ),
)
def test_full_playbook_run_via_home_to_signoff_to_workspace(watched_page, connected_app_url, tmp_path):
    page, watcher = watched_page
    # dcc.ConfirmDialog's sign-off confirmation is a native browser confirm()
    # popup; Playwright auto-dismisses (cancels) any dialog with no handler
    # registered, so accept it explicitly -- this is the real user action,
    # not a workaround.
    page.on("dialog", lambda d: d.accept())
    goto(page, connected_app_url, "/")

    # Upload a small CSV that binds to SKILL-001's smallest contract source
    # (per_diem_rates -- the same two columns and rows as the real
    # synthetic_data/ file, so contract validation passes for real) by exact
    # filename-stem match (run_setup._auto_bind, never fuzzy). Confirms the
    # landing page's upload panel is a real, working run source, not just a
    # decorative dropzone (reference_app's own L3 test documents it as
    # exactly that decorative no-op in the prototype).
    csv_path = tmp_path / "per_diem_rates.csv"
    csv_path.write_text("Country/Region,Rate Per Day (S$)\nAfghanistan,101\nAlbania,87\n")
    page.locator("#file-upload-area input[type=file]").set_input_files(str(csv_path))
    page.wait_for_selector("#uploaded-files-list .upload-file-row")
    assert page.get_by_text("Ready", exact=True).count() >= 1

    # Selecting the T&E Skill card: the prototype wires no selection
    # callback to skill_card at all (reproduced exactly here, see the
    # change report) -- the run always uses SKILL-001/Playbook, matching
    # the run-summary-preview panel's own hardcoded text below. Clicking it
    # is still the real user gesture the task asks for; it is a documented
    # no-op rather than a broken control.
    page.locator('[id*="skill-select-card"][id*="SKILL-001"]').first.click()

    page.locator("#audit-period input").first.fill("01/01/2025")
    page.keyboard.press("Enter")
    page.locator("#audit-period input").nth(1).fill("30/04/2026")
    page.keyboard.press("Enter")
    page.locator("#audit-objective").fill("Assess T&E spend for control exceptions.")

    page.locator("#start-run-btn").click()
    page.wait_for_url("**/run/RUN-*", timeout=15_000)

    assert not has_error_overlay(page)

    def _tick():
        # "load" rather than "networkidle": app.py's external_stylesheets
        # pulls Bootstrap from a CDN, and this sandbox's outbound network
        # policy fails that request (TLS interception, unrelated to this
        # app) -- a resource the browser keeps retrying can hold
        # "networkidle" open indefinitely. A generous explicit timeout
        # (Playwright's default is 30s): the profile/execute nodes hold the
        # GIL for tens of seconds at a time parsing the ~30MB
        # Expense_Report_Combined.xlsx (openpyxl, not chunked), which can
        # delay even a same-process static response.
        page.reload(wait_until="load", timeout=120_000)

    # Poll the run page through to whichever gate appears first. Playbook
    # auto-confirms and skips straight past awaiting_confirmation UNLESS
    # this deployment declares a column mapping/parameter for SKILL-001
    # (CLAUDE.md §11 "column mapping at run setup" forces plan confirmation
    # even in Playbook -- orchestrator.service.start_audit_run's own
    # has_run_inputs check); this deployment declares none today, so
    # waiting for either button only guards against that changing later.
    def _reached_confirmation_or_signoff_gate() -> bool:
        return (page.locator("#run-confirm-plan-btn").count() > 0
                or page.locator("#run-mark-prepared-btn").count() > 0)

    _poll_until(page, _reached_confirmation_or_signoff_gate, timeout_s=_PIPELINE_TIMEOUT_S, on_tick=_tick)

    if page.locator("#run-confirm-plan-btn").count() > 0:
        # UI-M1 (docs/specs/P7_mapping_authoring_design.md §1.4): a run with
        # declared inputs lists them here before confirmation -- not
        # asserted on since this deployment has none, but the click itself
        # is the same real user gesture either way.
        page.locator("#run-confirm-plan-btn").click()

        def _reached_signoff_gate() -> bool:
            return page.locator("#run-mark-prepared-btn").count() > 0

        _poll_until(page, _reached_signoff_gate, timeout_s=_PIPELINE_TIMEOUT_S, on_tick=_tick)

    # P7 review workflow (docs/specs/P7_mapping_authoring_design.md §3.3):
    # awaiting_signoff no longer shows "Sign off findings" directly -- a run
    # defaults to review stage 'preparation' and walks preparer -> reviewer
    # -> approver before the sign-off button appears. Each of these two
    # steps is a synchronous RunState write (orchestrator.runs.
    # prepare_findings / mark_reviewed), not a pipeline node, so the click's
    # own callback response updates the DOM directly -- no reload/poll
    # needed, matching app/tests/e2e_local/test_review_ui.py's own pattern
    # for the same two steps. connected_app_url's own REVIEW_SOD_MODE=
    # labelled / REVIEW_ROLE_SOURCE=config setup (CLAUDE.md §11 "Development
    # workspace: labelled mode") is what lets this single browser identity
    # hold every role.
    page.locator("#run-mark-prepared-btn").click()
    page.wait_for_selector("#run-mark-reviewed-btn")
    page.locator("#run-mark-reviewed-btn").click()
    page.wait_for_selector("#run-signoff-open-btn")

    # Single click: opens the native confirm() dialog, auto-accepted by the
    # page.on("dialog", ...) handler registered above, which fires
    # sign_off() synchronously in that same round trip.
    page.locator("#run-signoff-open-btn").click()

    def _completed() -> bool:
        return page.get_by_text("Run complete").count() > 0

    _poll_until(page, _completed, timeout_s=_EXPORT_TIMEOUT_S, on_tick=_tick)

    run_url = page.url
    run_id = run_url.rsplit("/", 1)[-1]

    watcher.assert_clean()

    # /workspace/tne for this run — every tab should render cleanly.
    goto(page, connected_app_url, f"/workspace/tne?run_id={run_id}")
    assert not has_error_overlay(page)

    tab_labels = ["Executive Brief", "Findings & Evidence", "Test catalogue", "Audit Detail", "Management Actions"]
    for label in tab_labels:
        tab = page.get_by_role("tab", name=label)
        assert tab.count() >= 1, f"tab {label!r} not found"
        tab.first.click()
        page.wait_for_timeout(400)
        assert not has_error_overlay(page)

    watcher.assert_clean()

    # XLSX download, from the run page (streams get_export — not rebuilt here).
    goto(page, connected_app_url, f"/run/{run_id}")
    with page.expect_download(timeout=30_000) as dl_info:
        page.locator("#run-download-xlsx-btn").click()
    download = dl_info.value
    assert download.suggested_filename.endswith(".xlsx")
