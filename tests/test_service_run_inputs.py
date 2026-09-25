"""Service-level integration for "run inputs" (independent review
2026-09-25 item 1, docs/specs/P7_mapping_authoring_design.md §1.3):
start_audit_run wired against a SOURCE_BINDINGS file declaring a
not_supplied source, exercised through the real local-backend executor --
the same harness tests/test_p3_service.py uses."""

from __future__ import annotations

from pathlib import Path

import yaml

from orchestrator import service
from tests.test_p3_service import _build_ctx, _wait_for_status


def _bindings_path(tmp_path: Path, config: dict) -> str:
    p = tmp_path / "source_bindings.yaml"
    p.write_text(yaml.safe_dump(config))
    return str(p)


def test_not_supplied_source_forces_plan_confirmation_and_run_completes(tmp_path):
    bindings_file = _bindings_path(tmp_path, {
        "SKILL-MINI": {"register": {"kind": "not_supplied", "reason": "not held by this business unit"}},
    })
    ctx = _build_ctx(tmp_path, env_overrides={"SOURCE_BINDINGS": bindings_file})
    ctx.executor.start()
    try:
        bindings = service.suggest_bindings(ctx, "SKILL-MINI")
        run_id = service.start_audit_run(
            ctx, skill_id="SKILL-MINI", bindings=bindings,
            audit_period=("2026-01-01", "2026-02-28"), objective="not-supplied run test",
            run_owner="tester",
        )

        # Mandatory plan confirmation (§1.3): auto_confirm_plan is forced off
        # by any declared run input, so the run stops at awaiting_confirmation
        # rather than racing straight past it.
        status = _wait_for_status(ctx, run_id, {"awaiting_confirmation", "failed"})
        run = service.get_run(ctx, run_id)
        assert status == "awaiting_confirmation", run.get("status_reason")
        assert run["options"]["run_inputs"]["not_supplied"]["register"]["reason"] == (
            "not held by this business unit"
        )
        assert run["options"]["run_inputs"]["not_supplied"]["register"]["affected_tests"] == ["T2"]

        service.confirm_plan(ctx, run_id, "tester")
        status = _wait_for_status(ctx, run_id, {"awaiting_signoff", "failed"})
        run = service.get_run(ctx, run_id)
        assert status == "awaiting_signoff", run.get("status_reason")

        test_results = {t["test_id"]: t for t in run["test_results"]}
        assert test_results["T1"]["status"] == "exception"
        assert test_results["T2"]["status"] == "not_testable"
        assert "register" in test_results["T2"]["reason"]

        # data_assets carries an explicit not_supplied entry, no fabricated binding.
        register_asset = next(a for a in run["data_assets"] if a["source"] == "register")
        assert register_asset["table_fqn"] is None
        assert register_asset["version"] is None
        assert register_asset["not_supplied"] == "not held by this business unit"

        service.sign_off(ctx, run_id, "approver")
        status = _wait_for_status(ctx, run_id, {"completed", "failed"})
        assert status == "completed", service.get_run(ctx, run_id).get("status_reason")
    finally:
        ctx.executor.stop()


def test_run_inputs_applied_trace_event_is_written(tmp_path):
    bindings_file = _bindings_path(tmp_path, {
        "SKILL-MINI": {"register": {"kind": "not_supplied", "reason": "x"}},
    })
    ctx = _build_ctx(tmp_path, env_overrides={"SOURCE_BINDINGS": bindings_file})
    ctx.executor.start()
    try:
        bindings = service.suggest_bindings(ctx, "SKILL-MINI")
        run_id = service.start_audit_run(
            ctx, skill_id="SKILL-MINI", bindings=bindings,
            audit_period=("2026-01-01", "2026-02-28"), objective="trace event test",
            run_owner="tester",
        )
        _wait_for_status(ctx, run_id, {"awaiting_confirmation", "failed"})
        events = service.list_trace_events(ctx, run_id=run_id)
        applied = [e for e in events if e["event_type"] == "run_inputs_applied"]
        assert len(applied) == 1
        assert "1 source(s) not supplied" in applied[0]["message"]
    finally:
        ctx.executor.stop()


def test_no_source_bindings_means_no_run_inputs_and_ordinary_auto_confirm(tmp_path):
    ctx = _build_ctx(tmp_path)
    ctx.executor.start()
    try:
        bindings = service.suggest_bindings(ctx, "SKILL-MINI")
        run_id = service.start_audit_run(
            ctx, skill_id="SKILL-MINI", bindings=bindings,
            audit_period=("2026-01-01", "2026-02-28"), objective="ordinary run",
            run_owner="tester",
        )
        status = _wait_for_status(ctx, run_id, {"awaiting_signoff", "failed"})
        run = service.get_run(ctx, run_id)
        assert status == "awaiting_signoff", run.get("status_reason")
        assert run["options"]["run_inputs"] == {"mappings": {}, "not_supplied": {}, "parameters": {}}
    finally:
        ctx.executor.stop()
