from __future__ import annotations

import pytest

from orchestrator.identity import RoleResolution
from orchestrator.signoff_policy import (
    STAGES,
    StepDecision,
    compute_sod_waived,
    evaluate_step,
    label_for,
    next_stage_for_action,
    role_for_action,
    stage_for_action,
)

ACTIONS = ("prepared", "reviewed", "approved")


def _resolution(*roles, source="workspace_groups"):
    return RoleResolution(
        roles=frozenset(roles),
        matched_groups={r: f"g-{r}" for r in roles},
        role_source=source,
    )


def test_role_and_stage_tables():
    assert role_for_action("prepared") == "preparer"
    assert role_for_action("reviewed") == "reviewer"
    assert role_for_action("approved") == "approver"
    assert stage_for_action("prepared") == "preparation"
    assert stage_for_action("reviewed") == "review"
    assert stage_for_action("approved") == "approval"
    assert next_stage_for_action("prepared") == "review"
    assert next_stage_for_action("reviewed") == "approval"
    assert next_stage_for_action("approved") is None


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("stage", STAGES)
def test_full_role_stage_action_matrix(action, stage):
    role = role_for_action(action)
    decision = evaluate_step(
        action, "alice", stage=stage, resolution=_resolution(role),
        configured_groups=(f"audit-{role}s",), prior_actors={}, open_notes=0,
        sod_mode="enforced",
    )
    if stage == stage_for_action(action):
        assert decision.allowed, (action, stage)
        assert decision.role == role
        assert decision.matched_group == f"g-{role}"
        assert decision.role_source == "workspace_groups"
    else:
        assert not decision.allowed, (action, stage)
        assert decision.reason


def test_wrong_role_refused():
    decision = evaluate_step(
        "prepared", "alice", stage="preparation", resolution=_resolution("reviewer"),
        configured_groups=("audit-preparers",), prior_actors={}, open_notes=0,
        sod_mode="enforced",
    )
    assert not decision.allowed
    assert "not in a preparer/reviewer/approver group" in decision.reason
    assert "audit-preparers" in decision.reason


def test_open_notes_block():
    decision = evaluate_step(
        "reviewed", "alice", stage="review", resolution=_resolution("reviewer"),
        configured_groups=("audit-reviewers",), prior_actors={}, open_notes=2,
        sod_mode="enforced",
    )
    assert not decision.allowed
    assert "2 open" in decision.reason


def test_sod_refused_in_enforced():
    decision = evaluate_step(
        "reviewed", "alice", stage="review", resolution=_resolution("reviewer"),
        configured_groups=("audit-reviewers",),
        prior_actors={"preparer": "alice"}, open_notes=0, sod_mode="enforced",
    )
    assert not decision.allowed
    assert "already acted on this run as preparer" in decision.reason


def test_sod_waived_in_labelled():
    decision = evaluate_step(
        "reviewed", "alice", stage="review", resolution=_resolution("reviewer"),
        configured_groups=("audit-reviewers",),
        prior_actors={"preparer": "alice"}, open_notes=0, sod_mode="labelled",
    )
    assert decision.allowed
    assert decision.role == "reviewer"


def test_sod_different_role_other_actor_never_blocks():
    decision = evaluate_step(
        "approved", "carol", stage="approval", resolution=_resolution("approver"),
        configured_groups=("audit-approvers",),
        prior_actors={"preparer": "alice", "reviewer": "bob"}, open_notes=0,
        sod_mode="enforced",
    )
    assert decision.allowed


def test_compute_sod_waived_empty_when_all_distinct():
    assert compute_sod_waived({"preparer": "alice", "reviewer": "bob", "approver": "carol"}) == []


def test_compute_sod_waived_pairs():
    waived = compute_sod_waived({"preparer": "alice", "reviewer": "bob", "approver": "alice"})
    assert waived == [["approver", "preparer"]]


def test_label_for_legacy_self_approved():
    assert label_for({"self_approved": True}) == "Self-approved — segregation of duties not enforced"


def test_label_for_legacy_not_self_approved():
    assert label_for({"self_approved": False}) is None


def test_label_for_p7_no_waiver():
    row = {"prepared_by": "alice", "reviewed_by": "bob", "approved_by": "carol", "sod_waived": []}
    assert label_for(row) is None


def test_label_for_p7_waived():
    row = {"prepared_by": "alice", "reviewed_by": "bob", "approved_by": "alice", "sod_waived": [["approver", "preparer"]]}
    assert label_for(row) == "Self-approved — segregation of duties not enforced"


def test_step_decision_is_frozen():
    d = StepDecision(True, role="preparer")
    with pytest.raises(Exception):
        d.role = "reviewer"
