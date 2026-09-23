"""flagged_rows.group_id (CLAUDE.md build brief P3 §1): the engine's
long-format flags (orchestrator/engine.py's ExecutionResult.flags_long) carry
group_id straight from each primitive, and the `execute` node persists
flagged_rows from that long frame rather than un-pivoting the wide RF_*
frame (which never carried group_id through the pivot). Exercised against
the real SKILL-001 planted fixture, whose T5.1 (split_detection) and T5.2
(duplicate_detection) tests are the two primitives that actually group rows
-- a plain per-row test (no group_by) correctly has a null group_id, which
is not what this file is testing."""

from __future__ import annotations

from orchestrator.nodes.fieldwork import discover, execute
from tests.test_p3_tne_gates import DATA_DIR, _make_ctx_and_state, pytestmark  # noqa: F401


def test_flagged_rows_carry_group_id_for_grouped_tests(local_persistence, uid):
    ctx, state = _make_ctx_and_state(local_persistence, DATA_DIR, run_id=f"RUN-GROUPID-{uid}")
    state = discover(ctx, state)
    state = execute(ctx, state)

    flagged = local_persistence.list_flagged_rows(state.run_id)
    by_flag: dict[str, list[dict]] = {}
    for r in flagged:
        by_flag.setdefault(r["flag"], []).append(r)

    for flag in ("RF_CS_SplitClaims_SameDay", "RF_CS_Duplicate"):
        rows = by_flag.get(flag, [])
        assert rows, f"no flagged rows for {flag} in the planted fixture"
        assert all(r["group_id"] for r in rows), f"{flag}: expected every row to carry a group_id, got {rows}"
