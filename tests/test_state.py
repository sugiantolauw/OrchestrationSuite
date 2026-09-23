from __future__ import annotations

import json
import math

import pytest

from orchestrator.errors import NotJsonSafe
from orchestrator.state import RunState, assert_json_safe, from_json, to_json, validate

FULL_STATE_KWARGS = dict(
    run_id="RUN-FULL",
    run_kind="fieldwork",
    engagement_id="ENG-1",
    skill_id="SKILL-001",
    skill_version="1.2",
    mode="playbook",
    phase="execute",
    next_node_index=3,
    audit_period=("2026-01-01", "2026-06-30"),
    objective="Test ExCo T&E controls",
    business_unit="Consumer",
    materiality=50000.0,
    options={"auto_confirm_plan": True, "nested": {"a": 1}},
    run_owner="sugianto.lauw@gmail.com",
    fingerprint_id="FP-ABC123",
    state_version=4,
    created_at="2026-01-01T00:00:00+00:00",
    started_at="2026-01-01T00:00:01+00:00",
    completed_at=None,
    last_state_change_at="2026-01-01T00:05:00+00:00",
    current_node_attempt_id="ATT-1",
    data_assets=[{"table": "expense_report", "version": "3"}],
    uploaded_files=[{"path": "/Volumes/x/f.csv", "sha256": "abc"}],
    profile_result={"row_count": 4200, "null_rates": {"col": 0.02}},
    plan={"tests": [{"primitive": "duplicate_detection"}]},
    plan_confirmed=True,
    plan_edits=[{"field": "threshold", "old": 10, "new": 20}],
    test_results=[{"test_id": "T5.1", "exceptions": 12}],
    flagged_table="run_1234_flags",
    reconciliation={"rows": 4200, "sum": 123456.78},
    exceptions=[{"row_key": "R1"}],
    findings=[{"id": "T4_1", "severity": "High"}],
    management_actions=[{"owner": "Finance", "due": "2026-03-01"}],
    exports={"pptx": "/Volumes/x/run.pptx"},
    signoff={"approver": "alice", "timestamp": "2026-02-01T00:00:00+00:00"},
    status_reason=None,
    profile_narrative="4,200 expense rows profiled.",
    plan_rationale={"T5.1": "split-claim window per policy"},
    classification_reasoning={"E1": "duplicate vendor+amount+date"},
    finding_narratives={"T4_1": "12 claims missing receipts"},
    priority_rationale={"T4_1": "highest exposure"},
    remediation_drafts={"T4_1": "enforce receipt attachment"},
    exec_summary="Overall control environment is adequate.",
    chart_captions={"chart1": "Exceptions by test"},
    events=[{"node": "discover", "ts": "t1"}],
    errors=[],
    status="running",
)


def _full_state() -> RunState:
    return RunState(**FULL_STATE_KWARGS)


def test_round_trip_exact():
    state = _full_state()
    payload = to_json(state)
    restored = from_json(payload)
    assert restored == state
    assert isinstance(restored.audit_period, tuple)


def test_to_json_is_valid_json_and_canonical():
    state = _full_state()
    payload = to_json(state)
    data = json.loads(payload)
    assert data["run_id"] == "RUN-FULL"
    assert data["audit_period"] == ["2026-01-01", "2026-06-30"]
    # canonical: sorted keys, no extra whitespace
    assert payload == json.dumps(data, sort_keys=True, separators=(",", ":"))


def test_defaults():
    state = RunState(
        run_id="R",
        run_kind="fieldwork",
        mode="playbook",
        phase="plan",
        audit_period=("2026-01-01", "2026-01-31"),
        objective="o",
        run_owner="alice",
        fingerprint_id="F",
        created_at="t0",
        last_state_change_at="t0",
        status="queued",
        engagement_id="E1",
    )
    assert state.next_node_index == 0
    assert state.plan_confirmed is False
    assert state.state_version == 0
    assert state.options == {}
    assert state.data_assets == []
    assert state.findings == []
    assert state.engagement_id == "E1"
    assert state.skill_id is None
    assert state.materiality is None


def test_assert_json_safe_rejects_set():
    state = _full_state()
    bad = state.__class__(**{**FULL_STATE_KWARGS, "options": {"bad": {1, 2, 3}}})
    with pytest.raises(NotJsonSafe) as exc:
        assert_json_safe(bad)
    assert "options" in str(exc.value)


def test_assert_json_safe_rejects_custom_object():
    class Weird:
        pass

    state = _full_state()
    bad = state.__class__(**{**FULL_STATE_KWARGS, "profile_result": {"x": Weird()}})
    with pytest.raises(NotJsonSafe) as exc:
        assert_json_safe(bad)
    assert "profile_result" in str(exc.value)


def test_assert_json_safe_rejects_nan():
    state = _full_state()
    bad = state.__class__(**{**FULL_STATE_KWARGS, "materiality": float("nan")})
    with pytest.raises(NotJsonSafe) as exc:
        assert_json_safe(bad)
    assert "materiality" in str(exc.value)


def test_assert_json_safe_rejects_infinity():
    state = _full_state()
    bad = state.__class__(**{**FULL_STATE_KWARGS, "materiality": math.inf})
    with pytest.raises(NotJsonSafe):
        assert_json_safe(bad)


def test_assert_json_safe_rejects_non_str_dict_key():
    state = _full_state()
    bad = state.__class__(**{**FULL_STATE_KWARGS, "options": {1: "x"}})
    with pytest.raises(NotJsonSafe):
        assert_json_safe(bad)


def test_validate_ok():
    validate(_full_state())


@pytest.mark.parametrize("run_kind", ["fieldwork", "assessment", "planning"])
def test_validate_requires_engagement_id_for_scoped_kinds(run_kind):
    state = RunState(
        run_id="R",
        run_kind=run_kind,
        mode="playbook",
        phase="plan",
        audit_period=("2026-01-01", "2026-01-31"),
        objective="o",
        run_owner="alice",
        fingerprint_id="F",
        created_at="t0",
        last_state_change_at="t0",
        status="queued",
        engagement_id=None,
    )
    with pytest.raises(ValueError, match="engagement_id"):
        validate(state)


def test_validate_allows_none_engagement_id_for_sensing():
    state = RunState(
        run_id="R",
        run_kind="sensing",
        mode="playbook",
        phase="plan",
        audit_period=("2026-01-01", "2026-01-31"),
        objective="o",
        run_owner="alice",
        fingerprint_id="F",
        created_at="t0",
        last_state_change_at="t0",
        status="queued",
        engagement_id=None,
    )
    validate(state)


def test_validate_rejects_bad_audit_period_order():
    state = _full_state()
    bad = state.__class__(**{**FULL_STATE_KWARGS, "audit_period": ("2026-06-30", "2026-01-01")})
    with pytest.raises(ValueError, match="audit_period"):
        validate(bad)


def test_validate_rejects_non_iso_dates():
    state = _full_state()
    bad = state.__class__(**{**FULL_STATE_KWARGS, "audit_period": ("not-a-date", "2026-01-31")})
    with pytest.raises(ValueError, match="audit_period"):
        validate(bad)


def test_validate_rejects_empty_run_owner():
    state = _full_state()
    bad = state.__class__(**{**FULL_STATE_KWARGS, "run_owner": ""})
    with pytest.raises(ValueError, match="run_owner"):
        validate(bad)


@pytest.mark.parametrize("field,value", [("run_kind", "bogus"), ("mode", "bogus"), ("phase", "bogus"), ("status", "bogus")])
def test_validate_rejects_bad_literals(field, value):
    state = _full_state()
    bad = state.__class__(**{**FULL_STATE_KWARGS, field: value})
    with pytest.raises(ValueError, match=field):
        validate(bad)
