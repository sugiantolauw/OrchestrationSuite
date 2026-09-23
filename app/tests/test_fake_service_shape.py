"""CLAUDE.md P2/P3 gate review item 2: fake_service.py must return the SAME
shape the real orchestrator.service returns -- get_run()'s findings are the
compact RunState.findings projection (no observation/recommendation/
metrics_cited), get_run_payload()'s findings are the full persisted rows.
This builds a real run through orchestrator.service on the local backend
(the "mini" Skill fixture, same pattern as tests/test_p3_service.py) and a
fake run through fake_service, then compares finding key sets between them
-- so a future edit that lets either shape drift is caught here rather than
only surfacing as an empty-looking card in the real app."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd
import pytest

_APP_TESTS_DIR = Path(__file__).resolve().parent
_APP_DIR = _APP_TESTS_DIR.parent
_REPO_ROOT = _APP_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator import service  # noqa: E402

import fake_service  # noqa: E402 -- already importable via app/tests/conftest.py's sys.path setup

MINI_SKILL_DIR = _REPO_ROOT / "tests" / "fixtures" / "skills" / "mini"


def _write_mini_data(root: Path) -> None:
    # Same fixture data as tests/test_p3_nodes.py's _write_mini_data -- kept
    # duplicated here (rather than imported) to avoid importing the
    # repo-root `tests` package from a directory that is ALSO named `tests`
    # (app/tests), which the sys.path ordering here makes ambiguous.
    claims = pd.DataFrame(
        [
            {"Employee ID": 1, "Transaction Date": "2026-01-05", "Amount": 100, "Vendor": "VendorA"},
            {"Employee ID": 1, "Transaction Date": "2026-01-06", "Amount": 600, "Vendor": "VendorA"},
            {"Employee ID": 2, "Transaction Date": "2026-01-10", "Amount": 700, "Vendor": "VendorB"},
            {"Employee ID": 3, "Transaction Date": "2026-01-15", "Amount": 50, "Vendor": "VendorC"},
            {"Employee ID": 4, "Transaction Date": "2026-02-01", "Amount": 900, "Vendor": "VendorD"},
        ]
    )
    register = pd.DataFrame(
        [
            {"Employee ID": 1, "Transaction Date": "2026-01-05", "Vendor": "VendorA"},
            {"Employee ID": 2, "Transaction Date": "2026-01-10", "Vendor": "VendorB"},
            {"Employee ID": 4, "Transaction Date": "2026-02-01", "Vendor": "VendorD"},
        ]
    )
    claims.to_csv(root / "claims.csv", index=False)
    register.to_csv(root / "register.csv", index=False)


def _build_real_ctx(tmp_path: Path) -> service.AppContext:
    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(tmp_path / "data"),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
        "ORCH_WORKER_ID": "shape-check-worker",
        "SKILLS_DIR": str(MINI_SKILL_DIR.parent),
        "CODE_REVISION": "test-fixed-revision",
    }
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    _write_mini_data(data_dir)
    return service.build_app_context(env)


def _wait_for_status(ctx, run_id, statuses, timeout=15):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = service.get_run(ctx, run_id)["status"]
        if last in statuses:
            return last
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach {statuses} in time (last={last})")


@pytest.fixture
def real_run(tmp_path):
    ctx = _build_real_ctx(tmp_path)
    ctx.executor.start()
    try:
        bindings = service.suggest_bindings(ctx, "SKILL-MINI")
        run_id = service.start_audit_run(
            ctx, skill_id="SKILL-MINI", bindings=bindings,
            audit_period=("2026-01-01", "2026-02-28"), objective="fake_service shape check",
            run_owner="tester",
        )
        status = _wait_for_status(ctx, run_id, {"awaiting_signoff", "failed"})
        assert status == "awaiting_signoff", service.get_run(ctx, run_id).get("status_reason")
        yield ctx, run_id
    finally:
        ctx.executor.stop()


@pytest.fixture
def fake_run():
    ctx = fake_service.build_app_context()
    run_id = fake_service.start_audit_run(
        ctx, skill_id="SKILL-001", bindings={"expense_report": "x"},
        audit_period=("2025-01-01", "2025-12-31"), objective="fake_service shape check",
        run_owner="tester", review_plan_first=False,
    )
    return ctx, run_id


def test_get_run_findings_are_the_compact_projection_in_both(real_run, fake_run):
    real_ctx, real_run_id = real_run
    fake_ctx, fake_run_id = fake_run

    real_findings = service.get_run(real_ctx, real_run_id)["findings"]
    fake_findings = fake_service.get_run(fake_ctx, fake_run_id)["findings"]
    assert real_findings, "real mini-skill run produced no findings to compare shapes against"
    assert fake_findings, "fake run produced no findings to compare shapes against"

    real_keys = set(real_findings[0].keys())
    fake_keys = set(fake_findings[0].keys())
    assert fake_keys == real_keys, (
        "fake_service.get_run()'s compact findings shape has diverged from the real "
        f"service's RunState.findings projection: real={sorted(real_keys)} "
        f"fake={sorted(fake_keys)} (CLAUDE.md P2/P3 gate review item 2)"
    )
    # The bug this whole item exists to catch: no card-rendering field on the
    # compact projection.
    for prose_field in ("observation", "recommendation", "management_questions", "metrics_cited"):
        assert prose_field not in real_keys, f"real get_run() findings unexpectedly carry {prose_field!r}"
        assert prose_field not in fake_keys, f"fake get_run() findings unexpectedly carry {prose_field!r}"


def test_get_run_payload_findings_carry_full_prose_in_both(real_run, fake_run):
    real_ctx, real_run_id = real_run
    fake_ctx, fake_run_id = fake_run

    real_findings = service.get_run_payload(real_ctx, real_run_id)["findings"]
    fake_findings = fake_service.get_run_payload(fake_ctx, fake_run_id)["findings"]
    assert real_findings, "real mini-skill run produced no findings to compare shapes against"
    assert fake_findings, "fake run produced no findings to compare shapes against"

    required_prose_fields = {
        "observation", "recommendation", "management_questions", "metrics_cited",
        "analyst_set_severity", "severity_basis", "exposure_amount", "exposure_basis",
    }
    real_keys = set(real_findings[0].keys())
    fake_keys = set(fake_findings[0].keys())
    assert required_prose_fields <= real_keys, sorted(required_prose_fields - real_keys)
    assert required_prose_fields <= fake_keys, (
        "fake_service.get_run_payload()'s findings shape is missing fields the real "
        f"service's full persisted row carries: {sorted(required_prose_fields - fake_keys)} "
        "(CLAUDE.md P2/P3 gate review item 2)"
    )
