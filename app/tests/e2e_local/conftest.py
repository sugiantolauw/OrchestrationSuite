"""Browser E2E fixtures for app/app.py against a REAL local backend
(ORCH_BACKEND=local, orchestrator.service -- never fake_service.py, and
never mocked at the adapters layer, since app/app.py runs in its own OS
subprocess here and cannot see anything this test process monkeypatches).

Round-3 browser testing (see BUG-RUNVIEW-1 in src/runs_page.py and the
management-action-edit regression this package's own test covers) found
real defects that unit tests calling callbacks directly missed entirely --
this package drives an actual Chromium browser against an actual running
Dash server instead.

Speed: every fixture run here uses tests/fixtures/skills/mini (a few
KB of CSV, no LLM calls -- see MODEL_SONNET/MODEL_GPT_OSS stripped from the
subprocess's env below) rather than the real SKILL-001 T&E Skill, which
needs real narration calls per CLAUDE.md §6 and takes minutes even before
a browser is involved. `running_app` is module-scoped: the whole package
shares one app.py subprocess and one small set of pre-created runs so the
suite's total wall-clock time stays in the seconds, not minutes."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[3]
MINI_SKILL_DIR = REPO_ROOT / "tests" / "fixtures" / "skills" / "mini"


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


# ── Chromium resolution (mirrors tests/e2e/conftest.py exactly) ────────────

def _chromium_executable() -> str | None:
    browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    base = Path(browsers_path)
    if not base.is_dir():
        return None
    candidates = sorted(base.glob("chromium-*/chrome-linux*/chrome"))
    if not candidates:
        return None
    return str(candidates[-1])


@pytest.fixture(scope="package")
def browser():
    with sync_playwright() as p:
        launch_kwargs = {"headless": True}
        exe = _chromium_executable()
        if exe:
            launch_kwargs["executable_path"] = exe
        b = p.chromium.launch(**launch_kwargs)
        yield b
        b.close()


_ENVIRONMENT_PROXY_NOISE = "net::ERR_CERT_AUTHORITY_INVALID"

# Pre-existing, out-of-scope noise: run_setup.py's own `Input("audit-
# objective", "value")`-driven callbacks are registered globally
# (run_setup.register_callbacks(app), app.py), so dash-renderer logs this
# "nonexistent object" console error on every page OTHER than "/" ("/runs",
# "/run/<id>", "/workspace/tne", ...) whose layout has no audit-objective
# element -- confirmed present before either bug this package tests for was
# touched, and unrelated to both (it is about a landing-page-only element
# id, not about runs-list re-rendering or management-action persistence).
# Fixing it is out of this task's scope; only filtered here so it cannot
# mask a genuine NEW console error one of these tests would otherwise catch.
_PREEXISTING_AUDIT_OBJECTIVE_NOISE = (
    "nonexistent object was used in an `Input` of a Dash callback. "
    "The id of this object is `audit-objective`"
)


class ConsoleWatcher:
    """Same shape as tests/e2e/conftest.py's own ConsoleWatcher: collects
    browser console errors and failed (>=400) Dash update-component
    responses while attached to a page. _ENVIRONMENT_PROXY_NOISE is this
    sandbox's own TLS-terminating egress proxy failing to fetch the
    Bootstrap CDN stylesheet (no CA in headless Chromium's profile), not an
    application defect."""

    def __init__(self, page):
        self.console_errors: list[str] = []
        self.failed_dash_responses: list[tuple[int, str]] = []
        page.on("console", self._on_console)
        page.on("response", self._on_response)

    def _on_console(self, msg):
        if msg.type != "error":
            return
        if _ENVIRONMENT_PROXY_NOISE in msg.text:
            return
        if _PREEXISTING_AUDIT_OBJECTIVE_NOISE in msg.text:
            return
        self.console_errors.append(msg.text)

    def _on_response(self, response):
        if "_dash-update-component" in response.url and response.status >= 400:
            self.failed_dash_responses.append((response.status, response.url))

    def assert_clean(self):
        assert not self.console_errors, f"Browser console errors: {self.console_errors}"
        assert not self.failed_dash_responses, (
            f"Failed _dash-update-component responses: {self.failed_dash_responses}"
        )


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    pg = context.new_page()
    pg.set_default_timeout(20_000)
    pg.set_default_navigation_timeout(20_000)
    yield pg
    context.close()


@pytest.fixture
def watched_page(page):
    watcher = ConsoleWatcher(page)
    return page, watcher


# P7 review workflow (docs/specs/P7_mapping_authoring_design.md §3.5, §3.10
# "Browser" test row): three identities, each its own browser context with a
# distinct `X-Forwarded-Email` header -- run_status._request_actor honours
# this header on the local backend exactly as the deployed backend honours
# the platform's own forwarded header, which is how these tests act as
# different people with no real identity provider (rule: "Automated tests
# never impersonate the real user").
IDENTITY_EMAILS = {
    "preparer": "e2e-preparer@example.invalid",
    "reviewer": "e2e-reviewer@example.invalid",
    "approver": "e2e-approver@example.invalid",
}


@pytest.fixture
def identity_pages(browser):
    contexts = {
        role: browser.new_context(viewport={"width": 1440, "height": 900}, extra_http_headers={"X-Forwarded-Email": email})
        for role, email in IDENTITY_EMAILS.items()
    }
    pages = {}
    for role, ctx in contexts.items():
        pg = ctx.new_page()
        pg.set_default_timeout(20_000)
        pg.set_default_navigation_timeout(20_000)
        pages[role] = pg
    yield pages
    for ctx in contexts.values():
        ctx.close()


def goto(page, base_url: str, path: str, wait_until: str = "networkidle"):
    page.goto(base_url + path, wait_until=wait_until)
    return page


def _strip_workspace_env(env: dict) -> dict:
    """Removes anything that would make the App reach a real Databricks
    workspace or a real Model Serving endpoint -- this package's fixture
    Skill (tests/fixtures/skills/mini) needs neither, and reaching either
    for real is exactly what makes the SKILL-001 pipeline take minutes
    (live GPT-OSS narration calls) instead of well under a second."""
    for k in (
        "MODEL_SONNET", "MODEL_GPT_OSS", "MODEL_ENDPOINT_HOST",
        "DATABRICKS_HOST", "DATABRICKS_TOKEN",
        "DBX_CATALOG", "DBX_SCHEMA", "DBX_VOLUME", "DBX_WAREHOUSE_HTTP_PATH",
        "DBX_SOURCE_SCHEMAS", "DBX_APP_NAME",
        # CLAUDE.md §11 perf follow-up 2026-09-26: this package's whole point
        # is a real, fast local backend -- the ambient dev .env may well set
        # STARTUP_WARMUP=true for the deployed App, and this subprocess's env
        # is `dict(os.environ)` merged with base_env (build_full_env above),
        # so an unstripped ambient value would leak in and spawn a background
        # warm-up thread this package neither needs nor wants racing the app
        # subprocess's own startup.
        "STARTUP_WARMUP",
    ):
        env.pop(k, None)
    return env


def build_full_env(base_env: dict) -> dict:
    """The one place `base_env` (this fixture's own small ORCH_*/REVIEW_*
    dict) is merged with the ambient environment -- P7 review-workflow gap
    found live: `running_app`'s seed-time AppContext used to be built from
    `dict(base_env)` ALONE, while the subprocess got `dict(os.environ)`
    merged with `base_env`. Any settings-affecting variable already exported
    into THIS session (e.g. NARRATION_ENABLED, AUDIT_TIMEZONE --
    CLAUDE.md's own dev-workspace .env sets both) then differed between the
    two, so `config.runtime_config_hash` differed between the run's stored
    fingerprint (computed at seed time) and the subprocess's own value --
    `executor._fingerprint_environment_matches` then silently refused
    admission forever (by design, for a genuinely different deployment;
    NOT the case here) the moment a run needed the subprocess's executor to
    run a node it had not already run at seed time -- invisible until this
    WP's test_review_ui.py became the first e2e_local test to click "Sign
    off findings" through a real browser and wait for the async export node
    to actually complete. Both the seed-time ctx and the subprocess now
    build from this SAME merged, stripped env."""
    full_env = dict(os.environ)
    _strip_workspace_env(full_env)
    full_env.update(base_env)
    return full_env


def _drain_output(proc: subprocess.Popen, maxlen: int = 4000) -> deque:
    """Merge-regression root cause (2026-09-26, e2e_local hang after the
    mapping + review-workflow merges): `proc.stdout` is a `subprocess.PIPE`
    that nothing ever read while the app subprocess ran. Werkzeug logs every
    HTTP request it serves (on by default, never silenced here), and
    `running_app` is a single long-lived subprocess shared by the whole
    package -- three browser contexts each polling `/run/<id>` on a 3s
    `dcc.Interval`, plus this package's own poll-by-reload helpers, easily
    produce enough log lines over a multi-minute test run to fill the OS
    pipe buffer (64KB on Linux). Once that buffer is full, the child's next
    write to stdout/stderr BLOCKS -- and because CPython's `logging`/stream
    machinery serializes writers through the handler's own lock, one thread
    blocked mid-write freezes every other thread in the app that also tries
    to log a line, wedging request handling entirely with no exception and
    no crash. That is what a bare `Page.goto: Timeout 20000ms exceeded`
    was actually reporting: not a slow request, a fully wedged server.
    Draining the pipe continuously on a background thread means it can
    never fill, so the app can never wedge itself just by logging."""
    lines: deque[str] = deque(maxlen=maxlen)

    def _pump() -> None:
        try:
            for line in proc.stdout:
                lines.append(line)
        except (ValueError, OSError):
            pass

    threading.Thread(target=_pump, daemon=True, name="e2e-local-log-drain").start()
    return lines


def _launch_app_subprocess(full_env: dict) -> tuple[subprocess.Popen, str]:
    """Factored out of running_app below so test_review_ui.py's App-restart
    check (§3.10's Browser test row) can launch and kill its OWN app.py
    process without disturbing the package-scoped `running_app` every other
    e2e_local test shares. `full_env` must already be `build_full_env`'s
    output -- this does not merge or strip again.

    The returned `proc` carries its drained log lines as `proc.e2e_log_lines`
    (see `_drain_output`) -- `_dump_server_log_on_failure` below reads this
    to print the server's own output when a test in this package fails,
    rather than a caller having to read `proc.stdout` itself (already fully
    consumed by the drain thread)."""
    port = _free_port()
    full_env = dict(full_env)
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


def _write_tiny_mini_data(root: Path) -> None:
    (root / "claims.csv").write_text(
        "Employee ID,Transaction Date,Amount,Vendor\n"
        "1,2026-01-05,600,Acme\n"
        "2,2026-01-10,900,Beta\n"
        "3,2026-01-15,50,Gamma\n"
    )
    (root / "register.csv").write_text(
        "Employee ID,Transaction Date,Vendor\n"
        "1,2026-01-05,Acme\n"
    )


@pytest.fixture(scope="package")
def running_app(tmp_path_factory):
    """Launches app/app.py for real against a throwaway LocalPersistence db
    seeded with a handful of completed SKILL-MINI runs (created directly
    via orchestrator.service, bypassing the browser -- fast setup, real
    persistence, real app process), then yields (base_url, run_ids,
    skills_dir, data_dir). One process for the whole package."""
    from orchestrator import service

    tmp = tmp_path_factory.mktemp("e2e_local")
    skills_dir = tmp / "skills"
    skills_dir.mkdir()
    shutil.copytree(MINI_SKILL_DIR, skills_dir / "mini")

    # A second, "SKILL-001-shaped" copy -- has_workspace=True (View on a
    # completed/awaiting-signoff run of THIS Skill must route to
    # /workspace/tne?run_id=<id>, CLAUDE.md §11 "Run cards on /runs"; only
    # the real skills/tne_exco/workspace.py exists today, but nothing in
    # _view_target (src/runs_page.py) is SKILL-001-specific -- it keys on
    # has_workspace alone, and that flag is exactly "does this Skill
    # directory have a workspace.py file" (orchestrator/service.py). This
    # is the same mini fixture, so it needs no real narration/model calls
    # either -- the empty workspace.py is never actually invoked (app.py's
    # /workspace/tne route always renders src/workspace_tne.py regardless
    # of Skill, CLAUDE.md §6 D5 "/workspace/tne stays SKILL-001's" -- View
    # only needs to prove it NAVIGATES there for a has_workspace Skill).
    ws_skill_dir = skills_dir / "mini_ws"
    shutil.copytree(MINI_SKILL_DIR, ws_skill_dir)
    (ws_skill_dir / "manifest.yaml").write_text(
        (ws_skill_dir / "manifest.yaml").read_text().replace("SKILL-MINI", "SKILL-MINI-WS")
    )
    (ws_skill_dir / "workspace.py").write_text("")

    data_dir = tmp / "data"
    data_dir.mkdir()
    _write_tiny_mini_data(data_dir)

    db_path = tmp / "orch.db"
    # P7 review workflow (docs/specs/P7_mapping_authoring_design.md §3.5):
    # "config" is local-backend/e2e-only -- REVIEW_ROLE_ASSIGNMENTS maps
    # each e2e-*@example.invalid identity to exactly one role, matching
    # test_review_ui.py's identity_pages fixture below. Additive: no
    # existing e2e_local test touches the P7 workflow at all (every run
    # they sign off goes through the LEGACY dual-path in orchestrator.runs.
    # sign_off, which never reads these), so enabling REVIEW_SOD_MODE=
    # enforced here changes nothing for them.
    review_roles_path = tmp / "review_roles.yaml"
    review_roles_path.write_text(
        "e2e-preparer@example.invalid: [preparer]\n"
        "e2e-reviewer@example.invalid: [reviewer]\n"
        "e2e-approver@example.invalid: [approver]\n"
    )
    base_env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(db_path),
        "SKILLS_DIR": str(skills_dir),
        "ORCH_LOCAL_DATA_ROOT": str(data_dir),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp / "exports"),
        "ORCH_WORKER_ID": "e2e-local",
        "CODE_REVISION": "test-fixed-revision",
        "REVIEW_ROLE_SOURCE": "config",
        "REVIEW_ROLE_ASSIGNMENTS": str(review_roles_path),
        "REVIEW_SOD_MODE": "enforced",
        # Only used for UI-R6's own refusal-message text (real role
        # resolution goes through REVIEW_ROLE_ASSIGNMENTS above regardless
        # of these) -- set for message-text fidelity with the real
        # .env.example defaults.
        "REVIEW_PREPARER_GROUPS": "audit-preparers",
        "REVIEW_REVIEWER_GROUPS": "audit-reviewers",
        "REVIEW_APPROVER_GROUPS": "audit-approvers",
    }

    full_env = build_full_env(base_env)
    ctx = service.build_app_context(dict(full_env))
    ctx.executor.start()
    run_ids: dict[str, object] = {}
    try:
        bindings = service.suggest_bindings(ctx, "SKILL-MINI")
        assert all(bindings.values()), bindings
        ws_bindings = service.suggest_bindings(ctx, "SKILL-MINI-WS")
        assert all(ws_bindings.values()), ws_bindings

        def _wait(ctx, rid, statuses, timeout=30):
            deadline = time.time() + timeout
            status = None
            while time.time() < deadline:
                status = service.get_run(ctx, rid)["status"]
                if status in statuses:
                    return status
                time.sleep(0.1)
            raise AssertionError(f"run {rid} did not reach {statuses} in {timeout}s (last={status})")

        def _run_to_completed(objective: str, skill_id: str = "SKILL-MINI", binds=None) -> str:
            rid = service.start_audit_run(
                ctx, skill_id=skill_id, bindings=binds or bindings,
                audit_period=("2026-01-01", "2026-01-31"),
                objective=objective, run_owner="tester",
            )
            _wait(ctx, rid, {"awaiting_signoff", "failed"})
            status = service.get_run(ctx, rid)["status"]
            if status == "awaiting_signoff":
                service.sign_off(ctx, rid, "tester")
                _wait(ctx, rid, {"completed", "failed"})
            return rid

        completed_id = _run_to_completed("e2e_local: completed run for View/Export/Trace/filters")
        run_ids["completed"] = completed_id

        run_ids["workspace_completed"] = _run_to_completed(
            "e2e_local: completed has_workspace run", skill_id="SKILL-MINI-WS", binds=ws_bindings,
        )

        bad_bindings = dict(bindings)
        bad_bindings["claims"] = str(data_dir / "does-not-exist.csv")
        try:
            failed_rid = service.start_audit_run(
                ctx, skill_id="SKILL-MINI", bindings=bad_bindings,
                audit_period=("2026-01-01", "2026-01-31"),
                objective="e2e_local: failed run for View routing", run_owner="tester",
            )
            _wait(ctx, failed_rid, {"failed", "awaiting_signoff", "completed"})
            run_ids["failed"] = failed_rid
        except Exception:
            run_ids["failed"] = None

        # A second, awaiting-signoff run: never signed off -- used to prove
        # Export's pre-signoff message (CLAUDE.md §11 "Message line") and to
        # give /runs' status filter something genuinely non-"Completed".
        pending_rid = service.start_audit_run(
            ctx, skill_id="SKILL-MINI", bindings=bindings,
            audit_period=("2026-01-01", "2026-01-31"),
            objective="e2e_local: awaiting signoff (export-before-signoff check)",
            run_owner="tester",
        )
        _wait(ctx, pending_rid, {"awaiting_signoff", "failed"})
        assert service.get_run(ctx, pending_rid)["status"] == "awaiting_signoff"
        run_ids["awaiting_signoff"] = pending_rid

        run_ids["completed_2"] = _run_to_completed("e2e_local: second completed run")

        # P7 review workflow (docs/specs/P7_mapping_authoring_design.md §3):
        # a THIRD awaiting_signoff run, never touched by any other e2e_local
        # test -- test_review_ui.py drives its full prepare/note/review/
        # sign-off cycle live through the browser (those actions are plain
        # CAS state writes, not pipeline nodes, so they need no executor --
        # only the initial run needs pre-seeding, the same as pending_rid
        # above).
        review_rid = service.start_audit_run(
            ctx, skill_id="SKILL-MINI", bindings=bindings,
            audit_period=("2026-01-01", "2026-01-31"),
            objective="e2e_local: P7 review workflow", run_owner="tester",
        )
        _wait(ctx, review_rid, {"awaiting_signoff", "failed"})
        assert service.get_run(ctx, review_rid)["status"] == "awaiting_signoff"
        run_ids["review_workflow"] = review_rid

        actions = service.list_management_actions(ctx, filters={"run_id": completed_id})
        assert actions, "the mini Skill's findings should have drafted a management action to edit"
        run_ids["action_id"] = actions[0]["action_id"]
    finally:
        ctx.executor.stop()

    proc, base_url = _launch_app_subprocess(full_env)

    yield {
        "base_url": base_url, "run_ids": run_ids,
        "skills_dir": skills_dir, "data_dir": data_dir, "db_path": db_path,
        "base_env": base_env, "env": full_env, "proc": proc,
    }

    _stop_app_subprocess(proc)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Standard pytest recipe for reading a test's own outcome from a
    fixture's teardown (`request.node.rep_call` below) -- needed by
    `_dump_server_log_on_failure` since a fixture cannot otherwise tell
    whether the test it wrapped passed or failed."""
    outcome = yield
    rep = outcome.get_result()
    setattr(item, "rep_" + rep.when, rep)


@pytest.fixture(autouse=True)
def _dump_server_log_on_failure(request):
    """The guard for the hang this package's own git history hit
    (`_drain_output`'s docstring): whatever a test in this package fails
    with -- a goto timeout, an assertion, anything -- print every app
    subprocess's own drained log tail alongside the failure, so a genuine
    server-side hang or crash is visible immediately (an exception, a
    request that never returned, nothing happening at all) instead of only
    a bare `Page.goto: Timeout 20000ms exceeded` with no server-side
    context to diagnose it from.

    Scans every fixture value the failing test used for a dict carrying a
    `proc` with `e2e_log_lines` (`_drain_output`'s marker) -- covers
    `running_app` here and test_run_inputs_ui.py's own module-scoped
    `mapped_run_app`, both, without either needing to be named explicitly.
    A test that launches a SECOND subprocess mid-test (test_review_ui.py's
    app-restart test) is not a fixture value, so it prints its own
    process's log where it launches one, for the same reason."""
    yield
    rep = getattr(request.node, "rep_call", None)
    if rep is None or not rep.failed:
        return
    for name, value in request.node.funcargs.items():
        if not isinstance(value, dict):
            continue
        proc = value.get("proc")
        log_lines = getattr(proc, "e2e_log_lines", None) if proc is not None else None
        if not log_lines:
            continue
        alive = proc.poll() is None
        tail = "".join(list(log_lines)[-200:])
        print(
            f"\n----- {name!r} server log tail (subprocess alive={alive}) -----\n"
            f"{tail}"
            f"----- end {name!r} server log tail -----\n"
        )
