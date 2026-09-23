"""Unit tests for orchestrator/signoff_policy.py -- the single place that
decides whether a sign-off counts as self-approved and whether segregation
of duties is enforced (CLAUDE.md §11 "accept all defaults, allow self
sign-off for now"; §2.4, §9C)."""

from __future__ import annotations

from orchestrator.signoff_policy import SOD_ENFORCED, evaluate_signoff


def test_self_approval_detected_when_actor_matches_run_owner():
    result = evaluate_signoff(actor="alice", run_owner="alice")
    assert result == {"self_approved": True, "sod_enforced": False}


def test_non_self_approval_when_actor_differs_from_run_owner():
    result = evaluate_signoff(actor="bob", run_owner="alice")
    assert result == {"self_approved": False, "sod_enforced": False}


def test_sod_not_enforced_until_p7():
    # SoD is never enforced today -- neither branch blocks the sign-off, and
    # flipping this single constant is what P7 changes (CLAUDE.md §2.4).
    assert SOD_ENFORCED is False
    assert evaluate_signoff(actor="alice", run_owner="alice")["sod_enforced"] is False
    assert evaluate_signoff(actor="bob", run_owner="alice")["sod_enforced"] is False


def test_comparison_is_exact_not_case_insensitive():
    # Identity strings are opaque here -- no normalisation, no fuzzy match
    # (CLAUDE.md NN14): "Alice" and "alice" are different identities unless
    # the caller normalises them before calling this.
    result = evaluate_signoff(actor="Alice", run_owner="alice")
    assert result["self_approved"] is False
