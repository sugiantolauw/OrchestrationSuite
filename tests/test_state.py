from __future__ import annotations

import json
import math

import pytest

from dataclasses import fields

from orchestrator.errors import NodeContractViolation, NotJsonSafe
from orchestrator.state import (
    LIFECYCLE,
    NODE_OWNED,
    RunState,
    apply_node_output,
    assert_json_safe,
    from_json,
    node_owned_json,
    to_json,
    validate,
)
from tests.conftest import canonical_ts

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
    phase_epoch=3,
    created_at="2026-01-01T00:00:00.000000Z",
    started_at="2026-01-01T00:00:01.000000Z",
    completed_at="2026-01-01T00:10:00.000000Z",
    last_state_change_at="2026-01-01T00:05:00.000000Z",
    current_node_attempt_id="ATT-1",
    data_assets=[{"table": "expense_report", "version": "3"}],
    uploaded_files=[{"path": "/Volumes/x/f.csv", "sha256": "abc"}],
    profile_result={"row_count": 4200, "null_rates": {"col": 0.02}},
    plan={"tests": [{"primitive": "duplicate_detection"}]},
    plan_confirmed=True,
    plan_edits=[{"field": "threshold", "old": 10, "new": 20}],
    confirmed_plan_hash="a" * 64,
    review={"stage": "review", "prepared": {"actor": "alice", "at": "2026-02-01T00:00:00.000000Z", "role_source": "config"}},
    test_results=[{"test_id": "T5.1", "exceptions": 12}],
    flagged_table="run_1234_flags",
    reconciliation={"rows": 4200, "sum": 123456.78},
    exceptions=[{"row_key": "R1"}],
    findings=[{"id": "T4_1", "severity": "High"}],
    management_actions=[{"owner": "Finance", "due": "2026-03-01"}],
    exports={"pptx": "/Volumes/x/run.pptx"},
    module_output={"kind": "sensing", "stop_reason": None},
    signoff={"approver": "alice", "timestamp": "2026-02-01T00:00:00+00:00"},
    status_reason="node 'execute' failed: simulated",
    profile_narrative="4,200 expense rows profiled.",
    plan_rationale={"T5.1": "split-claim window per policy"},
    classification_reasoning={"E1": "duplicate vendor+amount+date"},
    finding_narratives={"T4_1": "12 claims missing receipts"},
    priority_rationale={"T4_1": "highest exposure"},
    remediation_drafts={"T4_1": "enforce receipt attachment"},
    exec_summary="Overall control environment is adequate.",
    chart_captions={"chart1": "Exceptions by test"},
    events=[{"node": "discover", "ts": "t1"}],
    errors=[{"node": "execute", "error": "boom"}],
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
    assert state.phase_epoch == 1


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


@pytest.mark.parametrize("run_kind", ["fieldwork", "assessment", "planning", "design_assessment", "reporting", "evidence"])
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
        created_at=canonical_ts(0),
        last_state_change_at=canonical_ts(0),
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
        created_at=canonical_ts(0),
        last_state_change_at=canonical_ts(0),
        status="queued",
        engagement_id=None,
    )
    validate(state)


@pytest.mark.parametrize("run_kind", ["design_assessment", "reporting", "evidence"])
def test_validate_accepts_new_lifecycle_run_kinds_with_engagement_id(run_kind):
    state = RunState(
        run_id="R",
        run_kind=run_kind,
        mode="playbook",
        phase="plan",
        audit_period=("2026-01-01", "2026-01-31"),
        objective="o",
        run_owner="alice",
        fingerprint_id="F",
        created_at=canonical_ts(0),
        last_state_change_at=canonical_ts(0),
        status="queued",
        engagement_id="E1",
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


def test_validate_rejects_non_canonical_timestamp():
    state = _full_state()
    bad = state.__class__(**{**FULL_STATE_KWARGS, "created_at": "2026-01-01T00:00:00+00:00"})
    with pytest.raises(ValueError, match="created_at"):
        validate(bad)


def test_validate_rejects_non_canonical_audit_period_date():
    state = _full_state()
    bad = state.__class__(**{**FULL_STATE_KWARGS, "audit_period": ("2026-01-1", "2026-06-30")})
    with pytest.raises(ValueError, match="audit_period"):
        validate(bad)


def test_validate_rejects_phase_epoch_below_one():
    state = _full_state()
    bad = state.__class__(**{**FULL_STATE_KWARGS, "phase_epoch": 0})
    with pytest.raises(ValueError, match="phase_epoch"):
        validate(bad)


# ── NODE_OWNED / LIFECYCLE partition (B1) ───────────────────────────────────────


def test_node_owned_and_lifecycle_partition_all_fields_exactly_once():
    all_names = {f.name for f in fields(RunState)}
    assert NODE_OWNED | LIFECYCLE == all_names, (
        f"missing from either set: {all_names - (NODE_OWNED | LIFECYCLE)}"
    )
    assert NODE_OWNED & LIFECYCLE == set(), (
        f"present in both sets: {NODE_OWNED & LIFECYCLE}"
    )


def test_full_state_fixture_differs_from_every_default():
    import dataclasses as _dc

    for f in fields(RunState):
        if f.default is _dc.MISSING and f.default_factory is _dc.MISSING:
            continue  # required field, nothing to differ from
        default_value = f.default_factory() if f.default_factory is not _dc.MISSING else f.default
        actual = FULL_STATE_KWARGS[f.name]
        assert actual != default_value, f"{f.name}: fixture value equals the field default ({default_value!r})"


# ── apply_node_output (B1) ───────────────────────────────────────────────────────


def _minimal_state(**overrides) -> RunState:
    base = dict(
        run_id="R",
        run_kind="fieldwork",
        mode="playbook",
        phase="plan",
        audit_period=("2026-01-01", "2026-01-31"),
        objective="o",
        run_owner="alice",
        fingerprint_id="F",
        created_at=canonical_ts(0),
        last_state_change_at=canonical_ts(0),
        status="running",
        engagement_id="E1",
        state_version=5,
        next_node_index=2,
    )
    base.update(overrides)
    return RunState(**base)


def test_apply_node_output_takes_node_owned_from_result_and_lifecycle_from_current():
    current = _minimal_state(findings=[], events=[{"node": "discover"}])
    result = dataclasses_replace_findings(current, findings=[{"id": "T4_1"}], events=[{"node": "discover"}, {"node": "profile"}])
    # result also carries a different (bogus) lifecycle value, which must be ignored
    result = result.__class__(**{**result.__dict__, "next_node_index": 999, "state_version": 999})

    merged = apply_node_output(current, result)
    assert merged.findings == [{"id": "T4_1"}]
    assert merged.events == [{"node": "discover"}, {"node": "profile"}]
    # LIFECYCLE fields come from `current`, never from `result`
    assert merged.next_node_index == current.next_node_index
    assert merged.state_version == current.state_version


def dataclasses_replace_findings(state: RunState, **kwargs) -> RunState:
    import dataclasses as _dc

    return _dc.replace(state, **kwargs)


def test_apply_node_output_accepts_dict_node_result_for_recovery():
    current = _minimal_state(events=[{"node": "discover"}])
    owned = {name: getattr(current, name) for name in NODE_OWNED}
    owned["events"] = [{"node": "discover"}, {"node": "profile"}]
    owned["findings"] = [{"id": "T4_1"}]

    merged = apply_node_output(current, owned)
    assert merged.findings == [{"id": "T4_1"}]
    assert merged.events == owned["events"]
    assert merged.run_id == current.run_id  # lifecycle preserved


def test_apply_node_output_rejects_non_prefix_events():
    current = _minimal_state(events=[{"node": "discover"}, {"node": "profile"}])
    bad_result = dataclasses_replace_findings(current, events=[{"node": "discover"}, {"node": "DIFFERENT"}])
    with pytest.raises(NodeContractViolation, match="events"):
        apply_node_output(current, bad_result)


def test_apply_node_output_rejects_shortened_errors():
    current = _minimal_state(errors=[{"e": 1}, {"e": 2}])
    bad_result = dataclasses_replace_findings(current, errors=[{"e": 1}])
    with pytest.raises(NodeContractViolation, match="errors"):
        apply_node_output(current, bad_result)


def test_apply_node_output_allows_pure_append():
    current = _minimal_state(events=[{"node": "discover"}])
    result = dataclasses_replace_findings(current, events=[{"node": "discover"}, {"node": "profile"}])
    merged = apply_node_output(current, result)
    assert merged.events == [{"node": "discover"}, {"node": "profile"}]


def test_node_owned_json_contains_only_node_owned_fields():
    state = _minimal_state(findings=[{"id": "T4_1"}], plan={"tests": []})
    payload = node_owned_json(state)
    data = json.loads(payload)
    assert set(data.keys()) == NODE_OWNED
    assert data["findings"] == [{"id": "T4_1"}]


def test_node_owned_json_rejects_non_json_safe_owned_value():
    state = _minimal_state(profile_result={"x": {1, 2, 3}})
    with pytest.raises(NotJsonSafe):
        node_owned_json(state)


# ── module_output (L0.1) ─────────────────────────────────────────────────────


def test_module_output_is_node_owned():
    assert "module_output" in NODE_OWNED
    assert "module_output" not in LIFECYCLE


def test_module_output_defaults_to_empty_dict():
    state = _minimal_state()
    assert state.module_output == {}


def test_module_output_round_trips():
    state = _full_state()
    restored = from_json(to_json(state))
    assert restored.module_output == state.module_output == {"kind": "sensing", "stop_reason": None}


def test_apply_node_output_takes_module_output_from_result():
    current = _minimal_state(module_output={})
    result = dataclasses_replace_findings(current, module_output={"kind": "sensing", "coverage": {"docs": 3}})
    merged = apply_node_output(current, result)
    assert merged.module_output == {"kind": "sensing", "coverage": {"docs": 3}}


def test_node_owned_json_includes_module_output():
    state = _minimal_state(module_output={"kind": "planning", "stop_reason": "ceiling:max_llm_calls"})
    payload = node_owned_json(state)
    data = json.loads(payload)
    assert data["module_output"] == {"kind": "planning", "stop_reason": "ceiling:max_llm_calls"}


def test_pre_p7_json_without_review_key_loads_with_review_none():
    # P7 review workflow (docs/specs/P7_mapping_authoring_design.md §3.4):
    # RunState.review is a lifecycle field added after real runs existed --
    # a JSON payload from before this field existed simply omits the key,
    # and from_json's RunState(**data) must fall back to the field's own
    # default (None) rather than raise a missing-argument TypeError.
    state = _minimal_state()
    payload = json.loads(to_json(state))
    assert "review" in payload
    del payload["review"]  # simulates a row persisted before this field existed
    payload["audit_period"] = tuple(payload["audit_period"])

    loaded = RunState(**payload)
    assert loaded.review is None
