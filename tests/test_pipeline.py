from __future__ import annotations

import dataclasses

from orchestrator import runs
from orchestrator.pipeline import run_phase

# B1/B2 pipeline-level behaviour: node output ownership, fingerprint verification on
# every executor pass, and phase-epoch-scoped attempt recovery (CLAUDE.md §9C).


def _fingerprint(fp_id="FP-PL"):
    return dict(
        fingerprint_id=fp_id,
        source_table_versions="{}",
        uploaded_file_hashes="{}",
        reference_data_hashes="{}",
        skill_content_hash=None,
        code_revision="rev1",
        dependency_lock_hash="dep1",
        runtime_config_hash="rc1",
        endpoint_config="{}",
        prompt_template_version="none",
        created_at="2026-01-01T00:00:00.000000Z",
    )


def _create(persistence, clock, fp):
    return runs.create_run(
        persistence, run_kind="fieldwork", engagement_id="ENG-DEFAULT", skill_id=None,
        skill_version=None, mode="playbook", audit_period=("2026-01-01", "2026-01-31"),
        objective="t", run_owner="alice", fingerprint=fp, now=clock(),
    )


# ── B1: node output ownership ────────────────────────────────────────────────────────────


def test_node_lifecycle_field_mutation_fails_the_node_and_run(local_persistence, clock):
    persistence = local_persistence
    fp = _fingerprint()

    def rogue_node(skill, state):
        # A node may only write NODE_OWNED fields (findings, events, ...). Mutating a
        # LIFECYCLE field like run_owner is a contract violation the pipeline must
        # catch itself, not trust the node to avoid.
        return dataclasses.replace(state, run_owner="SOMEONE-ELSE")

    nodes_for = {"fieldwork": {"plan": [("discover", rogue_node)], "execute": [], "export": []}}
    state = _create(persistence, clock, fp)

    final = run_phase(persistence, state.run_id, nodes_for=nodes_for, clock=clock, current_fingerprint=fp)
    assert final.status == "failed"
    assert "NodeContractViolation" in (final.status_reason or "")
    assert final.run_owner == "alice"  # the rogue write never landed in persisted state

    attempts = persistence.list_node_attempts(state.run_id)
    assert attempts[0]["outcome"] == "failed"
    assert "NodeContractViolation" in attempts[0]["error_detail"]
    assert final.current_node_attempt_id == attempts[0]["attempt_id"]


def test_node_may_append_to_events_and_errors(local_persistence, clock):
    persistence = local_persistence
    fp = _fingerprint()

    def appending_node(skill, state):
        return dataclasses.replace(
            state,
            events=state.events + [{"node": "discover"}],
            errors=state.errors + [{"warning": "no receipts column"}],
            findings=[{"id": "T4_1"}],
        )

    nodes_for = {"fieldwork": {"plan": [("discover", appending_node)], "execute": [], "export": []}}
    state = _create(persistence, clock, fp)

    final = run_phase(persistence, state.run_id, nodes_for=nodes_for, clock=clock, current_fingerprint=fp)
    assert final.status == "awaiting_confirmation"
    assert final.events == [{"node": "discover"}]
    assert final.errors == [{"warning": "no receipts column"}]
    assert final.findings == [{"id": "T4_1"}]


def test_node_rewriting_events_instead_of_appending_fails(local_persistence, clock):
    persistence = local_persistence
    fp = _fingerprint()

    def bad_node(skill, state):
        # Replaces rather than extends -- violates the append-only contract on events.
        return dataclasses.replace(state, events=[{"node": "REWRITTEN"}])

    nodes_for = {"fieldwork": {"plan": [("discover", bad_node)], "execute": [], "export": []}}
    state = _create(persistence, clock, fp)
    seeded = dataclasses.replace(persistence.load_state(state.run_id), events=[{"node": "seed"}])
    persistence.save_state(seeded)

    final = run_phase(persistence, state.run_id, nodes_for=nodes_for, clock=clock, current_fingerprint=fp)
    assert final.status == "failed"
    assert "NodeContractViolation" in (final.status_reason or "")


def test_result_state_json_holds_only_node_owned_fields(local_persistence, clock):
    persistence = local_persistence
    fp = _fingerprint()

    def discover_fn(skill, state):
        return dataclasses.replace(state, data_assets=[{"table": "expense_report"}])

    nodes_for = {"fieldwork": {"plan": [("discover", discover_fn)], "execute": [], "export": []}}
    state = _create(persistence, clock, fp)
    run_phase(persistence, state.run_id, nodes_for=nodes_for, clock=clock, current_fingerprint=fp)

    from orchestrator.state import NODE_OWNED
    import json

    attempt = persistence.list_node_attempts(state.run_id)[0]
    owned = json.loads(attempt["result_state_json"])
    assert set(owned.keys()) == NODE_OWNED
    assert owned["data_assets"] == [{"table": "expense_report"}]


# ── fingerprint verification on every executor pass ──────────────────────────────────────


def test_fingerprint_mismatch_fails_run_without_executing_any_node(local_persistence, clock):
    persistence = local_persistence
    calls = {"n": 0}

    def counting_node(skill, state):
        calls["n"] += 1
        return state

    nodes_for = {"fieldwork": {"plan": [("discover", counting_node)], "execute": [], "export": []}}
    fp = _fingerprint("FP-MATCH")
    state = _create(persistence, clock, fp)

    drifted = _fingerprint("FP-MATCH")
    drifted["code_revision"] = "different-rev"

    final = run_phase(persistence, state.run_id, nodes_for=nodes_for, clock=clock, current_fingerprint=drifted)
    assert final.status == "failed"
    assert "code_revision" in (final.status_reason or "")
    assert calls["n"] == 0  # never ran a node


def test_matching_fingerprint_allows_the_run_to_proceed(local_persistence, clock):
    persistence = local_persistence
    fp = _fingerprint("FP-OK")
    nodes_for = {"fieldwork": {"plan": [("discover", lambda skill, state: state)], "execute": [], "export": []}}
    state = _create(persistence, clock, fp)

    final = run_phase(persistence, state.run_id, nodes_for=nodes_for, clock=clock, current_fingerprint=_fingerprint("FP-OK"))
    assert final.status == "awaiting_confirmation"


# ── B2: phase-epoch scoped attempts ───────────────────────────────────────────────────────


def test_reentering_plan_phase_at_new_epoch_reexecutes_nodes(local_persistence, clock):
    persistence = local_persistence
    call_counts: dict[str, int] = {}

    def counting_node(name):
        def fn(skill, state):
            call_counts[name] = call_counts.get(name, 0) + 1
            return dataclasses.replace(state, events=state.events + [{"node": name}])
        return fn

    nodes_for = {
        "fieldwork": {
            "plan": [(n, counting_node(n)) for n in ("discover", "profile")],
            "execute": [], "export": [],
        }
    }
    fp = _fingerprint()
    state = _create(persistence, clock, fp)

    state = run_phase(persistence, state.run_id, nodes_for=nodes_for, clock=clock, current_fingerprint=fp)
    assert state.status == "awaiting_confirmation"
    assert call_counts == {"discover": 1, "profile": 1}
    first_epoch = state.phase_epoch

    # No production path re-enters 'plan' from 'awaiting_confirmation' yet (CLAUDE.md
    # §9C/B2) -- construct the new epoch directly via persistence + transition helpers,
    # simulating what a future re-plan feature would produce.
    forced = dataclasses.replace(state, phase="plan", phase_epoch=state.state_version + 1, status="queued", next_node_index=0)
    state = persistence.save_state(forced)
    assert state.phase_epoch != first_epoch
    assert state.phase_epoch == state.state_version  # matches the "entered this phase at this version" invariant
    second_epoch = state.phase_epoch

    state = run_phase(persistence, state.run_id, nodes_for=nodes_for, clock=clock, current_fingerprint=fp)
    assert state.status == "awaiting_confirmation"
    # Both nodes re-executed at the new epoch -- the old epoch's succeeded attempts are
    # not mistaken for this epoch's work and silently replayed.
    assert call_counts == {"discover": 2, "profile": 2}

    discover_attempts = [a for a in persistence.list_node_attempts(state.run_id) if a["node_name"] == "discover"]
    assert len(discover_attempts) == 2
    assert {a["phase_epoch"] for a in discover_attempts} == {first_epoch, second_epoch}
    assert all(a["outcome"] == "succeeded" for a in discover_attempts)
    # execution_key is unique per epoch even though attempt_number resets to 1 in each
    assert len({a["execution_key"] for a in discover_attempts}) == 2


# ── trace event vocabulary ────────────────────────────────────────────────────────────────


def test_node_trace_events_use_ui_status_vocabulary_and_stage_labels(local_persistence, clock):
    persistence = local_persistence
    fp = _fingerprint()
    nodes_for = {"fieldwork": {"plan": [("discover", lambda skill, state: state)], "execute": [], "export": []}}
    state = _create(persistence, clock, fp)
    run_phase(persistence, state.run_id, nodes_for=nodes_for, clock=clock, current_fingerprint=fp)

    events = [e for e in persistence.list_trace_events(state.run_id) if e["node_name"] == "discover"]
    by_type = {e["event_type"]: e for e in events}
    assert by_type["node_started"]["status"] == "running"
    assert by_type["node_completed"]["status"] == "complete"
    assert by_type["node_started"]["stage"] == "Source data"


def test_node_completed_trace_event_carries_the_nodes_own_message(local_persistence, clock):
    # Independent review 2026-09-24 (CLAUDE.md §9B scenario 8): every
    # orchestrator/nodes/fieldwork.py node computes and appends a specific,
    # informative message to RunState.events (e.g. act's "Management
    # actions not generated (option off)" or "N management action(s)
    # drafted") -- CLAUDE.md §4.2's node table names exactly this kind of
    # label as each node's "Event". The persisted node_completed
    # trace_events row must carry that message, not the generic
    # "<node> completed" placeholder that discards it -- the /trace page
    # reads only trace_events, so a discarded message is invisible to an
    # auditor, not merely uncosmetic.
    persistence = local_persistence
    fp = _fingerprint()

    def fake_node(skill, state):
        return dataclasses.replace(
            state, events=state.events + [{"node": "discover", "message": "3 source(s) bound", "at": clock()}]
        )

    nodes_for = {"fieldwork": {"plan": [("discover", fake_node)], "execute": [], "export": []}}
    state = _create(persistence, clock, fp)
    run_phase(persistence, state.run_id, nodes_for=nodes_for, clock=clock, current_fingerprint=fp)

    events = [e for e in persistence.list_trace_events(state.run_id) if e["node_name"] == "discover"]
    by_type = {e["event_type"]: e for e in events}
    assert by_type["node_completed"]["message"] == "3 source(s) bound"
