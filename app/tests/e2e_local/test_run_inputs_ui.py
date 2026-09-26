"""Browser E2E for "run inputs" (independent review 2026-09-25 item 1,
docs/specs/P7_mapping_authoring_design.md §1.6 L-M1-shaped local check):
a run created against a SOURCE_BINDINGS file that declares one SKILL-MINI
source not_supplied stops at /run/<id>'s awaiting_confirmation gate, UI-M1's
lines are visible, and clicking "Confirm plan" drives the run through to
completion. Self-contained (its own app.py subprocess, its own db) rather
than sharing this package's module-scoped `running_app` fixture, so it can
set SOURCE_BINDINGS without affecting any other e2e_local test."""

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
import yaml

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


def _strip_workspace_env(env: dict) -> dict:
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


@pytest.fixture(scope="module")
def mapped_run_app(tmp_path_factory):
    """Sets up a single SKILL-MINI run, over its own db, with `register`
    declared not_supplied via a SOURCE_BINDINGS file -- stopped BEFORE
    confirm_plan, so it is still awaiting_confirmation when app.py's own
    subprocess (started with the same db and SOURCE_BINDINGS) serves
    /run/<id> to the browser below."""
    from orchestrator import service

    tmp = tmp_path_factory.mktemp("e2e_run_inputs")
    skills_dir = tmp / "skills"
    skills_dir.mkdir()
    shutil.copytree(MINI_SKILL_DIR, skills_dir / "mini")

    data_dir = tmp / "data"
    data_dir.mkdir()
    _write_tiny_mini_data(data_dir)

    bindings_path = tmp / "source_bindings.yaml"
    bindings_path.write_text(yaml.safe_dump({
        "SKILL-MINI": {"register": {"kind": "not_supplied", "reason": "not held by this business unit"}},
    }))

    db_path = tmp / "orch.db"
    base_env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(db_path),
        "SKILLS_DIR": str(skills_dir),
        "ORCH_LOCAL_DATA_ROOT": str(data_dir),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp / "exports"),
        "ORCH_WORKER_ID": "e2e-run-inputs",
        "CODE_REVISION": "test-fixed-revision",
        "SOURCE_BINDINGS": str(bindings_path),
    }

    # The in-process setup ctx and the app.py subprocess below must build the
    # IDENTICAL runtime_config_hash (orchestrator.config.runtime_config_hash)
    # or confirm_plan (run inside the subprocess) fails with
    # RunCodeRevisionStale the instant it re-verifies this run's fingerprint
    # -- same env construction as the subprocess: strip workspace secrets
    # from the ambient environment, then overlay base_env.
    setup_env = dict(os.environ)
    _strip_workspace_env(setup_env)
    setup_env.update(base_env)
    ctx = service.build_app_context(setup_env)
    ctx.executor.start()
    run_id = None
    try:
        bindings = service.suggest_bindings(ctx, "SKILL-MINI")
        run_id = service.start_audit_run(
            ctx, skill_id="SKILL-MINI", bindings=bindings,
            audit_period=("2026-01-01", "2026-01-31"),
            objective="e2e_local: run inputs UI test", run_owner="tester",
        )
        deadline = time.time() + 15
        status = None
        while time.time() < deadline:
            status = service.get_run(ctx, run_id)["status"]
            if status in ("awaiting_confirmation", "failed"):
                break
            time.sleep(0.1)
        assert status == "awaiting_confirmation", status
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

    yield {"base_url": base_url, "run_id": run_id, "proc": proc}

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def test_mapped_run_shows_ui_m1_lines_and_confirm_completes(mapped_run_app, page):
    base_url = mapped_run_app["base_url"]
    run_id = mapped_run_app["run_id"]

    page.goto(f"{base_url}/run/{run_id}", wait_until="networkidle")
    text = page.inner_text("body")
    assert "This run uses project-specific inputs, so the plan must be confirmed." in text
    assert "register not supplied" in text
    assert "not held by this business unit" in text
    assert "T2" in text

    page.click("#run-confirm-plan-btn")

    # Independent review 2026-09-25 (run-page freeze after Confirm plan /
    # Sign off / Resume / Regenerate, app/src/run_status.py): confirm_plan
    # moves the run into `queued` for the execute phase, and the SAME
    # callback round trip that renders that now re-enables run-poll's own
    # dcc.Interval (it was disabled while this page sat on the
    # awaiting_confirmation gate, CLAUDE.md P3 cost fix) -- so the page
    # reaches "Findings are ready for sign-off" on its own, from the
    # background poll alone, with NO reload. This is the regression test
    # for that freeze: before the fix, this would time out here exactly the
    # way the reload-based workaround this test used to need proves the bug
    # existed.
    try:
        page.wait_for_selector("text=Findings are ready for sign-off", timeout=60_000)
    except Exception:
        proc = mapped_run_app["proc"]
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=10)
        except Exception:
            out = "<no output captured>"
        raise AssertionError(
            f"page text after confirm click, no reload: {page.inner_text('body')!r}\n\n"
            f"server output:\n{out}"
        )
