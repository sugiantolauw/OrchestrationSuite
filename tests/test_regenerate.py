"""T-R1 (docs/specs/P6_narration_design.md §11, §5.5): `regenerate_narration`
bumps `phase_epoch`, restarts the execute phase at `narrate` (so a further
executor pass re-runs only `narrate` and `act`), keeps rule findings' own
existence/numbers/severities byte-identical, supersedes this run's
undecided AI-proposed candidates without deleting them, leaves decided
candidates untouched, and returns to `awaiting_signoff`. P6 WP N9,
`orchestrator.service.regenerate_narration` /
`orchestrator.runs.regenerate_narration` / `orchestrator.status.transition`'s
`restart_at_index`.

Note on scope (see this WP's final report): `orchestrator.nodes.narration.
narrate` (WP N7, merged) does not yet implement §5.5's "re-narrates every
narrative whose current origin is not human_edit" skip -- that belongs to
whichever WP finishes the regenerate-time narrate() behaviour, and
`nodes/`/`narration/` are out of this WP's scope to edit. So "keeps human
edits" is tested here at the level WP N9 actually owns: `regenerate_
narration` itself (the service/state-machine layer) never touches the
`narratives` table -- a human edit survives the CALL untouched. Surviving a
subsequent real `narrate()` re-execution is a property of nodes/narration.py,
not exercised here.

Second scope note, load-bearing for every test below except the canary:
`orchestrator.nodes.registry.NODES_FOR` -- the mapping `service.py` (and the
real executor) actually use to find `narrate`'s index -- was never updated
when WP N7 added `narrate` to `nodes/fieldwork.py`'s OWN (otherwise unused)
copy; see `test_the_node_registry_is_missing_narrate_blocking_regeneration`
below and this WP's final report. Fixing `orchestrator/nodes/registry.py`
is out of this WP's scope (`nodes/` is off-limits here), so every other
test monkeypatches `service.NODES_FOR` to the complete, correct list
(`orchestrator.nodes.fieldwork.NODES_FOR`) for its duration -- proving WP
N9's OWN logic is correct and ready the moment that registry gap is closed,
without silently working around a bug that belongs to a different WP."""

from __future__ import annotations

import pytest

from orchestrator import service
from orchestrator.pipeline import run_phase
from tests.n9_test_support import FIELDWORK_NODES_FOR, harness_at_awaiting_signoff


def test_the_node_registry_is_missing_narrate_blocking_regeneration(local_persistence, tmp_path, clock):
    """Canary, not a WP N9 defect: documents the real, currently-unfixed gap
    in `orchestrator/nodes/registry.py` (out of this WP's scope) that makes
    `regenerate_narration` unusable end-to-end against the actual production
    node lookup today. If this test starts failing, the registry gap has
    been fixed -- remove it and the `monkeypatch.setattr(service, "NODES_FOR",
    ...)` calls below stop being necessary (though they remain harmless)."""
    from orchestrator.errors import NarrationNodeUnavailable

    h, ctx_app, state = harness_at_awaiting_signoff(local_persistence, tmp_path, clock)
    with pytest.raises(NarrationNodeUnavailable):
        service.regenerate_narration(ctx_app, state.run_id, "alice")


@pytest.fixture
def with_complete_node_registry(monkeypatch):
    """Swaps in `orchestrator.nodes.fieldwork.NODES_FOR` (the complete node
    list, `narrate` included) in place of the incomplete registry-backed one
    `service.py` imports at module load, for the duration of one test."""
    monkeypatch.setattr(service, "NODES_FOR", FIELDWORK_NODES_FOR)


def _findings_signature(findings: list[dict]) -> list[tuple]:
    return sorted(
        (f["finding_id"], f["rule_id"], f["severity"], f["severity_basis"], tuple(sorted((f.get("metrics_cited") or {}).items())))
        for f in findings
    )


def test_regenerate_bumps_phase_epoch_and_restarts_at_narrate(local_persistence, tmp_path, clock, with_complete_node_registry):
    h, ctx_app, state = harness_at_awaiting_signoff(local_persistence, tmp_path, clock)
    run_id = state.run_id
    execute_nodes = FIELDWORK_NODES_FOR["fieldwork"]["execute"]
    node_names = [n for n, _ in execute_nodes]
    narrate_index = node_names.index("narrate")

    findings_before = h.persistence.list_findings(run_id)
    metrics_before = h.persistence.get_run_metrics(run_id)
    signature_before = _findings_signature(findings_before)

    new_state = service.regenerate_narration(ctx_app, run_id, "alice")

    assert new_state.status == "queued"
    assert new_state.phase == "execute"  # never changes phase
    assert new_state.next_node_index == narrate_index
    assert new_state.phase_epoch == state.state_version + 1
    assert new_state.phase_epoch > state.phase_epoch
    assert new_state.options["narration_generation"] == 1
    assert new_state.options["narration_generation_requested_by"] == "alice"

    events = [e for e in h.persistence.list_trace_events(run_id) if e["event_type"] == "narration_regenerate_requested"]
    assert len(events) == 1
    assert "generation 1" in events[0]["message"]

    # The call itself never touches rule findings or run_metrics.
    assert _findings_signature(h.persistence.list_findings(run_id)) == signature_before
    assert h.persistence.get_run_metrics(run_id) == metrics_before


def test_regenerate_then_executor_pass_reruns_only_narrate_and_act(local_persistence, tmp_path, clock, with_complete_node_registry):
    h, ctx_app, state = harness_at_awaiting_signoff(local_persistence, tmp_path, clock)
    run_id = state.run_id

    attempts_before = h.persistence.list_node_attempts(run_id)
    attempt_numbers_before = {a["node_name"]: a["attempt_number"] for a in attempts_before}
    findings_before = h.persistence.list_findings(run_id)
    signature_before = _findings_signature(findings_before)

    service.regenerate_narration(ctx_app, run_id, "alice")
    fingerprint = h.persistence.get_fingerprint(state.fingerprint_id)
    final_state = run_phase(
        h.persistence, run_id, nodes_for=FIELDWORK_NODES_FOR, skill=h.ctx, clock=clock, current_fingerprint=fingerprint,
    )

    assert final_state.status == "awaiting_signoff"
    assert final_state.phase == "execute"

    attempts_after = h.persistence.list_node_attempts(run_id)
    # attempt_number is scoped per phase_epoch (CLAUDE.md §4.1's own
    # execution-key shape, `run_id:phase:phase_epoch:node_name:attempt`), so
    # a node re-run under the NEW phase_epoch regenerate() produced starts
    # again at attempt_number 1 -- a second phase_epoch entirely, not a
    # second attempt_number within the old one. What distinguishes "ran
    # again" from "did not" is the COUNT of rows for that node, and that the
    # later row's phase_epoch is strictly greater.
    counts_after: dict[str, int] = {}
    max_epoch_after: dict[str, int] = {}
    for a in attempts_after:
        counts_after[a["node_name"]] = counts_after.get(a["node_name"], 0) + 1
        max_epoch_after[a["node_name"]] = max(max_epoch_after.get(a["node_name"], 0), a["phase_epoch"])
    epoch_before = {a["node_name"]: a["phase_epoch"] for a in attempts_before}

    # find/prioritise/classify/execute never ran a second attempt -- only
    # narrate and act did (§5.5: "runs only narrate and act").
    for node in ("execute", "classify", "find", "prioritise"):
        assert counts_after[node] == 1, node
        assert max_epoch_after[node] == epoch_before[node], node
    for node in ("narrate", "act"):
        assert counts_after[node] == 2, node
        assert max_epoch_after[node] > epoch_before[node], node

    # Rule findings' existence/numbers/severities are untouched by regenerate.
    assert _findings_signature(h.persistence.list_findings(run_id)) == signature_before


def _candidate(run_id: str, *, candidate_id: str, rule_id: str) -> dict:
    return {
        "candidate_id": candidate_id, "engagement_id": "ENG-DEFAULT", "skill_id": "SKILL-MINI",
        "generation": 0, "rule_id": rule_id, "title": "An AI-proposed candidate finding",
        "metrics_cited": ["hv_count"], "producing_test_ids": ["T1"], "proposed_severity": "Medium",
        "severity_reason": "Model-proposed reason.", "rationale": "Model rationale.",
        "monetary_basis": "none", "monetary_basis_note": "no declared monetary basis",
        "exposure_amount": None, "headline_eligible": False,
        "headline_ineligible_reason": "monetary_basis is none", "candidate_status": "candidate",
        "call_id": f"CALL-{candidate_id}",
    }


def test_regenerate_supersedes_undecided_candidates_but_keeps_decided_ones(local_persistence, tmp_path, clock, with_complete_node_registry):
    h, ctx_app, state = harness_at_awaiting_signoff(local_persistence, tmp_path, clock)
    run_id = state.run_id
    h.persistence.write_candidates(
        run_id,
        [
            _candidate(run_id, candidate_id="C-DECIDED", rule_id="SKILL-MINI.ai.aaa"),
            _candidate(run_id, candidate_id="C-UNDECIDED", rule_id="SKILL-MINI.ai.bbb"),
        ],
        now=clock(),
    )
    decided = service.decide_candidate(
        ctx_app, run_id, "C-DECIDED", decision="accepted", reason=None, decided_severity="High", actor="alice",
    )
    assert decided["candidate_status"] == "accepted"

    service.regenerate_narration(ctx_app, run_id, "bob")

    by_id = {c["candidate_id"]: c for c in h.persistence.list_candidates(run_id)}
    # Never deleted -- both rows are still present.
    assert set(by_id) == {"C-DECIDED", "C-UNDECIDED"}
    # Decided candidates are frozen: untouched by the regenerate.
    assert by_id["C-DECIDED"]["candidate_status"] == "accepted"
    assert by_id["C-DECIDED"]["decided_by"] == "alice"
    assert by_id["C-DECIDED"]["decided_severity"] == "High"
    # Undecided candidates from the prior generation are superseded.
    assert by_id["C-UNDECIDED"]["candidate_status"] == "superseded"
    assert by_id["C-UNDECIDED"]["decided_by"] is None


def test_regenerate_call_itself_never_touches_narratives_a_human_edit_survives_the_call(local_persistence, tmp_path, clock, with_complete_node_registry):
    # See this file's module docstring: end-to-end survival through a real
    # narrate() re-execution is nodes/narration.py's concern (WP N7/N8,
    # unmerged behaviour), out of this WP's scope. What WP N9 owns is that
    # its OWN transition never reaches into narratives -- asserted directly.
    h, ctx_app, state = harness_at_awaiting_signoff(local_persistence, tmp_path, clock)
    run_id = state.run_id
    narratives_before = {n["narrative_id"]: dict(n) for n in h.persistence.get_narratives(run_id)}
    edits_before = h.persistence.list_narrative_edits(run_id)

    service.regenerate_narration(ctx_app, run_id, "alice")

    narratives_after = {n["narrative_id"]: dict(n) for n in h.persistence.get_narratives(run_id)}
    assert narratives_after == narratives_before
    assert h.persistence.list_narrative_edits(run_id) == edits_before
