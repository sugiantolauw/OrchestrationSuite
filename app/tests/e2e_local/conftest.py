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
import time
import urllib.error
import urllib.request
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
    ):
        env.pop(k, None)
    return env


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
    base_env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(db_path),
        "SKILLS_DIR": str(skills_dir),
        "ORCH_LOCAL_DATA_ROOT": str(data_dir),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp / "exports"),
        "ORCH_WORKER_ID": "e2e-local",
        "CODE_REVISION": "test-fixed-revision",
    }

    ctx = service.build_app_context(dict(base_env))
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

        actions = service.list_management_actions(ctx, filters={"run_id": completed_id})
        assert actions, "the mini Skill's findings should have drafted a management action to edit"
        run_ids["action_id"] = actions[0]["action_id"]
    finally:
        ctx.executor.stop()

    port = _free_port()
    env = dict(os.environ)
    _strip_workspace_env(env)
    env.update(base_env)
    env["PORT"] = str(port)

    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "app" / "app.py")],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_http_200(base_url + "/ready", timeout=30.0)
    except Exception:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate(timeout=10)
        raise RuntimeError(f"app/app.py failed to start on {base_url}:\n{out}")

    yield {
        "base_url": base_url, "run_ids": run_ids,
        "skills_dir": skills_dir, "data_dir": data_dir, "db_path": db_path,
    }

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
