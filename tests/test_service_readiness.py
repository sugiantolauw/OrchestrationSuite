"""orchestrator.service's readiness wiring (independent review 2026-09-24
item 5): build_app_context wires a CachedReadiness onto AppContext,
service.ready()/readiness_report() use it, and start_audit_run refuses to
start while a required check is failing -- surfaced through RunNotReady,
which app/src/run_setup.py's existing generic exception handler already
turns into a message in the run-summary-preview panel."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator import service
from orchestrator.errors import RunNotReady
from tests.test_p3_nodes import MINI_SKILL_DIR, _write_mini_data


def _env(tmp_path: Path, **overrides) -> dict:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_mini_data(data_dir)
    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(data_dir),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
        "SKILLS_DIR": str(MINI_SKILL_DIR.parent),
    }
    env.update(overrides)
    return env


def test_build_app_context_wires_a_readiness_prober(tmp_path):
    ctx = service.build_app_context(_env(tmp_path))
    assert ctx.readiness is not None


def test_ready_reports_all_checks_when_healthy(tmp_path):
    ctx = service.build_app_context(_env(tmp_path))
    payload = service.ready(ctx)
    assert payload["ready"] is True
    assert payload["backend"] == "local"
    assert "checks" in payload


def test_readiness_report_matches_ready(tmp_path):
    ctx = service.build_app_context(_env(tmp_path))
    a = service.ready(ctx)
    b = service.readiness_report(ctx)
    assert a["ready"] == b["ready"]


def test_ready_falls_back_when_no_readiness_prober_wired():
    # An older-style direct AppContext(...) construction (e.g. hand-built in
    # a test) with readiness left at its default None must still work.
    class _Persistence:
        def find_runs(self, statuses):
            return []

    ctx = service.AppContext(
        settings=None, persistence=_Persistence(), skills_dir=Path("."),
        data_source_factory=lambda *a, **kw: None, export_storage=None, clock=lambda: "t",
    )
    payload = service.ready(ctx)
    assert payload["ready"] is True
    assert "detail" in payload


def test_start_audit_run_refuses_when_a_required_check_fails(tmp_path):
    ctx = service.build_app_context(_env(tmp_path))

    class _FailingReadiness:
        def get(self, force=False):
            from orchestrator.readiness import CheckResult, ReadinessReport

            return ReadinessReport(
                checks=(CheckResult("warehouse", False, "warehouse unreachable"),),
                checked_at="t",
            )

    ctx.readiness = _FailingReadiness()
    with pytest.raises(RunNotReady) as exc_info:
        service.start_audit_run(
            ctx, skill_id="SKILL-MINI", bindings={"claims": "claims.csv", "register": "register.csv"},
            audit_period=("2026-01-01", "2026-02-28"), objective="test", run_owner="alice",
        )
    assert "warehouse" in str(exc_info.value)


def test_start_audit_run_proceeds_despite_a_non_blocking_model_endpoint_failure(tmp_path):
    # Independent review 2026-09-24 item 2: no fieldwork node calls a model
    # endpoint unless `enable_row_level_llm` is on (only `classify`'s
    # optional T4.3 path does, and only then), so a model-endpoint check
    # failure alone -- tagged "feature:row_level_llm", not "always" -- must
    # not block a fieldwork run that never reaches an LLM.
    ctx = service.build_app_context(_env(tmp_path))

    class _DegradedReadiness:
        def get(self, force=False):
            from orchestrator.readiness import CheckResult, ReadinessReport

            return ReadinessReport(
                checks=(
                    CheckResult("warehouse", True),
                    CheckResult(
                        "model_endpoint:model_gpt_oss", False, "endpoint unreachable",
                        required_for=frozenset({"feature:row_level_llm"}),
                    ),
                ),
                checked_at="t",
            )

    ctx.readiness = _DegradedReadiness()
    run_id = service.start_audit_run(
        ctx, skill_id="SKILL-MINI", bindings={"claims": "claims.csv", "register": "register.csv"},
        audit_period=("2026-01-01", "2026-02-28"), objective="test", run_owner="alice",
    )
    assert run_id


def test_start_audit_run_proceeds_when_readiness_is_ok(tmp_path):
    ctx = service.build_app_context(_env(tmp_path))
    run_id = service.start_audit_run(
        ctx, skill_id="SKILL-MINI", bindings={"claims": "claims.csv", "register": "register.csv"},
        audit_period=("2026-01-01", "2026-02-28"), objective="test", run_owner="alice",
    )
    assert run_id
