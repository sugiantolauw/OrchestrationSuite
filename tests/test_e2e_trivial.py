from __future__ import annotations

import dataclasses
from pathlib import Path

from orchestrator import runs
from orchestrator.fingerprint import compute_fingerprint
from orchestrator.config import load_settings
from orchestrator.pipeline import run_phase

REPO_ROOT = Path(__file__).resolve().parent.parent


def _trivial_node(name):
    def fn(skill, state):
        return dataclasses.replace(state, events=state.events + [{"node": name}])

    return fn


NODES_FOR = {
    "fieldwork": {
        "plan": [(n, _trivial_node(n)) for n in ("discover", "profile", "plan")],
        "execute": [(n, _trivial_node(n)) for n in ("execute", "classify", "find", "prioritise", "act")],
        "export": [(n, _trivial_node(n)) for n in ("export",)],
    }
}

ALL_NODE_NAMES = [n for phase in NODES_FOR["fieldwork"].values() for n, _ in phase]


def test_trivial_fieldwork_playbook_run_end_to_end(persistence, clock, tmp_path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "manifest.yaml").write_text("id: SKILL-001\nversion: 1\n")

    settings = load_settings({})
    fingerprint = compute_fingerprint(
        settings=settings,
        source_table_versions={"catalog.schema.expense_report": "1"},
        uploaded_file_hashes={},
        skill_dir=skill_dir,
        requirements_path=REPO_ROOT / "requirements.txt",
        prompts_dirs=[],
    )

    state = runs.create_run(
        persistence,
        run_kind="fieldwork",
        engagement_id="ENG-DEFAULT",
        skill_id="SKILL-001",
        skill_version="1.0",
        mode="playbook",
        audit_period=("2026-01-01", "2026-01-31"),
        objective="ExCo T&E audit",
        run_owner="alice",
        fingerprint=fingerprint,
        now=clock(),
    )
    assert state.status == "queued"

    state = run_phase(persistence, state.run_id, nodes_for=NODES_FOR, clock=clock)
    assert state.status == "awaiting_confirmation"
    assert state.phase == "plan"

    state = runs.confirm_plan(persistence, state.run_id, actor="alice", now=clock())
    assert state.status == "queued"
    assert state.phase == "execute"
    assert state.plan_confirmed is True

    state = run_phase(persistence, state.run_id, nodes_for=NODES_FOR, clock=clock)
    assert state.status == "awaiting_signoff"
    assert state.phase == "execute"

    state = runs.sign_off(persistence, state.run_id, actor="alice", now=clock())
    assert state.status == "queued"
    assert state.phase == "export"
    assert state.signoff == {"approver": "alice", "timestamp": state.signoff["timestamp"]}

    state = run_phase(persistence, state.run_id, nodes_for=NODES_FOR, clock=clock)
    assert state.status == "completed"
    assert state.phase == "export"
    assert state.completed_at is not None

    attempts = persistence.list_node_attempts(state.run_id)
    assert len(attempts) == 9
    assert all(a["outcome"] == "succeeded" for a in attempts)
    assert [a["node_name"] for a in attempts] == ALL_NODE_NAMES

    events = persistence.list_trace_events(state.run_id)
    event_types = [e["event_type"] for e in events]
    assert "run_created" in event_types
    assert "plan_confirmed" in event_types
    assert "signed_off" in event_types
    assert event_types.count("node_started") == 9
    assert event_types.count("node_completed") == 9
    for e in events:
        for key in ("event_id", "run_id", "timestamp", "stage", "status", "message", "duration_s"):
            assert key in e

    timestamps = [e["timestamp"] for e in events]
    assert timestamps == sorted(timestamps)

    versions = [a["state_version_after"] for a in attempts]
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)

    assert [ev["node"] for ev in state.events] == ALL_NODE_NAMES
