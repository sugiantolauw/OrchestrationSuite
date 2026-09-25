"""Statement-count regression tests for cold-load latency on /runs, /skills,
/skills/<id>, /actions, /trace and /workspace/tne (P3/P4 perf gap review
2026-09-25, CLAUDE.md "Count statements per page without a workspace").

Rendered against a REAL orchestrator.service + LocalPersistence (mirroring
app/tests/test_runs_page.py's own `real_ctx`/`_make_run` pattern exactly),
never app/tests/fake_service.py -- fake_service has no real backing store,
so it cannot show an N+1 query pattern at all. Every test asserts a
STATEMENT BUDGET (a fixed upper bound, independent of how many runs/skills
exist) via statement_counting.count_statements, which traces every real SQL
statement LocalPersistence's sqlite connection actually executes -- proving
"renders with <= N statements regardless of population size", not merely
"renders correctly", is what catches an N+1 that a correctness-only test
would miss entirely (see the module docstrings this task's fixes reference:
BUG-RUNVIEW-1 in src/runs_page.py, the list_skills N+1 in orchestrator/
service.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from statement_counting import count_statements

from orchestrator.state import RunState
from orchestrator.timeutil import utc_now
from src.platform import adapters

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _fingerprint(fp_id: str) -> dict:
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
        created_at=utc_now(),
    )


def _make_run(
    ctx, run_id: str, *, status: str = "queued", skill_id: str | None = "SKILL-001",
    mode: str = "playbook", objective: str = "Assess spend.",
) -> None:
    """See app/tests/test_runs_page.py's own `_make_run` -- the same direct
    `ctx.persistence.create_run(state, fingerprint)` shortcut, reused here
    rather than duplicated with different field defaults."""
    now = utc_now()
    state = RunState(
        run_id=run_id,
        run_kind="fieldwork",
        engagement_id="ENG-DEFAULT",
        skill_id=skill_id,
        mode=mode,
        phase="plan",
        audit_period=("2026-01-01", "2026-01-31"),
        objective=objective,
        run_owner="alice",
        fingerprint_id=f"FP-{run_id}",
        created_at=now,
        last_state_change_at=now,
        status=status,
    )
    ctx.persistence.create_run(state, _fingerprint(f"FP-{run_id}"))


@pytest.fixture
def real_ctx(monkeypatch, tmp_path):
    from orchestrator import service as real_service

    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(tmp_path),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
        "SKILLS_DIR": str(_REPO_ROOT / "skills"),
    }
    monkeypatch.setattr(adapters, "service", real_service)
    adapters._ctx = None
    original_build = real_service.build_app_context
    monkeypatch.setattr(real_service, "build_app_context", lambda *a, **k: original_build(env))

    ctx = adapters.get_context()
    try:
        yield ctx
    finally:
        ctx.executor.stop()
        adapters._ctx = None


@pytest.fixture
def multi_skill_ctx(monkeypatch, tmp_path):
    """A second, third and fourth skill directory alongside the repo's real
    skills/tne_exco (symlinked in, same trick app/tests/test_skills_page.py
    uses) -- proves a statement budget holds as the SKILL COUNT grows, not
    only the run count. Each fixture skill carries its own manifest.yaml
    only (list_skills reads catalogue.yaml/plan.yaml too, but both are
    optional -- _read_yaml returns {} for a missing file)."""
    from orchestrator import service as real_service

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    import os

    os.symlink(_REPO_ROOT / "skills" / "tne_exco", skills_dir / "tne_exco")
    for i in range(3):
        d = skills_dir / f"fixture_{i}"
        d.mkdir()
        (d / "manifest.yaml").write_text(
            f"id: SKILL-FIXTURE-{i}\nname: Fixture Skill {i}\ndomain: Test\n"
            f"version: \"0.1.0\"\nowner: Test Owner\nstatus: draft\n"
        )

    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(tmp_path),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
        "SKILLS_DIR": str(skills_dir),
    }
    monkeypatch.setattr(adapters, "service", real_service)
    adapters._ctx = None
    original_build = real_service.build_app_context
    monkeypatch.setattr(real_service, "build_app_context", lambda *a, **k: original_build(env))

    ctx = adapters.get_context()
    try:
        yield ctx
    finally:
        ctx.executor.stop()
        adapters._ctx = None


# ── /runs ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("n_runs", [1, 30])
def test_runs_page_statement_budget_does_not_scale_with_run_count(real_ctx, n_runs):
    from src.platform.pages import audit_runs_page

    for i in range(n_runs):
        _make_run(real_ctx, f"RUN-{i}", status="completed" if i % 2 else "queued")

    with count_statements(real_ctx.persistence) as counter:
        audit_runs_page()

    # A handful of batched, whole-page reads (list_runs, list_skills,
    # findings/actions/metrics-for-runs, fingerprints) -- never one per run.
    assert counter.count <= 10, (
        f"audit_runs_page() issued {counter.count} statements for {n_runs} runs "
        f"(budget 10, independent of run count):\n" + "\n".join(counter.statements)
    )


def test_runs_filter_callback_does_not_fire_on_initial_mount():
    """BUG-RUNVIEW-1 (src/runs_page.py): the fix is a registered
    prevent_initial_call=True on the callback that owns runs-list.children
    -- asserted directly against dash's own per-callback registration
    (app._callback_list), the same mechanism dash-renderer itself consults
    to decide whether to fire a callback on initial page load."""
    from conftest import load_app_entry

    entry = load_app_entry()
    matches = [c for c in entry.app._callback_list if c["output"] == "runs-list.children"]
    assert matches and all(c["prevent_initial_call"] for c in matches)


# ── /skills ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("n_runs", [1, 15])
def test_skills_page_statement_budget_does_not_scale_with_skill_or_run_count(multi_skill_ctx, n_runs):
    from src.platform.pages import skill_library_page

    for i in range(n_runs):
        _make_run(multi_skill_ctx, f"RUN-{i}", skill_id="SKILL-001" if i % 2 else "SKILL-FIXTURE-0")

    with count_statements(multi_skill_ctx.persistence) as counter:
        skill_library_page()

    # list_runs + one full skill_versions read (list_all_skill_versions) --
    # never list_skill_versions(skill_id) once per skill directory plus a
    # SECOND full scan for list_skill_versions_by_origin (the N+2 this fixes).
    assert counter.count <= 4, (
        f"skill_library_page() issued {counter.count} statements for 4 skill dirs, "
        f"{n_runs} runs (budget 4):\n" + "\n".join(counter.statements)
    )
    skill_versions_reads = [s for s in counter.statements if "FROM skill_versions" in s]
    assert len(skill_versions_reads) <= 1, (
        f"expected at most one skill_versions read, got {len(skill_versions_reads)}:\n"
        + "\n".join(skill_versions_reads)
    )


def test_skills_filter_callback_does_not_fire_on_initial_mount():
    """BUG-SKILLS-2 (src/skills_page.py) -- see test_runs_filter_callback_
    does_not_fire_on_initial_mount's own docstring for why this is asserted
    at the dash registration level rather than by calling the callback."""
    from conftest import load_app_entry

    entry = load_app_entry()
    matches = [c for c in entry.app._callback_list if c["output"] == "skill-library-grid.children"]
    assert matches and all(c["prevent_initial_call"] for c in matches)


# ── /skills/<id> ─────────────────────────────────────────────────────────


def test_skill_detail_page_statement_budget(real_ctx):
    from src.platform.pages import skill_methodology_page

    with count_statements(real_ctx.persistence) as counter:
        skill_methodology_page("SKILL-001")

    # BUG-SKILLDETAIL-1 (P3/P4 perf gap review 2026-09-25, live pass):
    # get_skill(skill_id) now reads skill_versions ONCE and exposes this
    # skill's own rows as `version_history_rows` -- methodology.py no
    # longer issues a second, separate list_skill_versions(skill_id) read
    # against the same table (real count: skill_versions + list_runs = 2).
    # Budget kept a little above that, and still a small, fixed number of
    # reads, not one per version or one per test in the catalogue.
    assert counter.count <= 4, (
        f"skill_methodology_page('SKILL-001') issued {counter.count} statements "
        f"(budget 4):\n" + "\n".join(counter.statements)
    )
    skill_versions_reads = [s for s in counter.statements if "FROM skill_versions" in s]
    assert len(skill_versions_reads) <= 1, (
        f"expected at most one skill_versions read (BUG-SKILLDETAIL-1), got "
        f"{len(skill_versions_reads)}:\n" + "\n".join(skill_versions_reads)
    )


# ── /actions ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("n_runs", [1, 30])
def test_actions_page_statement_budget_does_not_scale_with_run_count(real_ctx, n_runs):
    from src.platform.pages import management_actions_page

    for i in range(n_runs):
        _make_run(real_ctx, f"RUN-{i}", status="completed" if i % 2 else "queued")

    with count_statements(real_ctx.persistence) as counter:
        management_actions_page()

    # BUG-ACTIONS-3 (P3/P4 perf gap review 2026-09-25, live pass):
    # management_actions_page() used to fetch the unfiltered runs list AND
    # the full skill_versions table TWICE each -- once inside
    # list_management_actions' own list_skills(ctx) call, once again for
    # cross_run_totals' list_audit_runs() -- measured live against the real
    # warehouse at 0.9-3.2s per skill_versions read alone. adapters.
    # get_actions_page_data() now fetches both raw reads ONCE and threads
    # them into both list_management_actions and list_runs (which also
    # threads them into its own internal list_skills call). Real count with
    # LocalPersistence: raw list_runs(1) + raw list_all_skill_versions(1) +
    # list_management_actions(1) + list_runs's 4 parallel-fanned-out reads
    # (findings/actions/metrics/fingerprints, LocalPersistence stays
    # sequential) = 7. Budget kept a little above that for headroom.
    assert counter.count <= 9, (
        f"management_actions_page() issued {counter.count} statements for {n_runs} runs "
        f"(budget 9, independent of run count):\n" + "\n".join(counter.statements)
    )


def test_actions_filter_callback_does_not_fire_on_initial_mount():
    """BUG-ACTIONS-2 (src/actions_page.py)."""
    from conftest import load_app_entry

    entry = load_app_entry()
    matches = [c for c in entry.app._callback_list if c["output"] == "actions-table-body.children"]
    assert matches and all(c["prevent_initial_call"] for c in matches)


# ── /trace ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("n_events", [1, 40])
def test_trace_page_statement_budget_does_not_scale_with_event_count(real_ctx, n_events):
    from src.platform.pages import platform_trace_page

    run_id = "RUN-A"
    _make_run(real_ctx, run_id)
    for i in range(n_events):
        real_ctx.persistence.append_trace_event({
            "event_id": f"EVT-{i}", "run_id": run_id, "engagement_id": "ENG-DEFAULT",
            "event_type": "node_completed", "event_time": utc_now(), "stage": "discover",
            "status": "complete", "message": f"event {i}", "duration_s": 0.1,
            "node_name": None, "execution_key": None, "actor": "system", "state_version": 1,
        })

    with count_statements(real_ctx.persistence) as counter:
        platform_trace_page()

    # adapters.list_audit_runs() (the dropdown's own run options -- a small,
    # fixed handful of batched reads) plus one trace_events read -- never
    # one statement per event.
    assert counter.count <= 10, (
        f"platform_trace_page() issued {counter.count} statements for {n_events} events "
        f"(budget 10, independent of event count):\n" + "\n".join(counter.statements)
    )


def test_trace_page_preselected_run_id_is_already_filtered_server_side(real_ctx):
    """BUG-TRACE-2: platform_trace_page(run_id=...) must render the
    filtered table itself now (src/platform/pages.py), since the filter
    callback no longer fires on initial mount to do it -- proven here by the
    rendered table's own content, not by re-inspecting SQL text."""
    from src.platform.pages import platform_trace_page

    _make_run(real_ctx, "RUN-A")
    _make_run(real_ctx, "RUN-B")
    real_ctx.persistence.append_trace_event({
        "event_id": "EVT-A1", "run_id": "RUN-A", "engagement_id": "ENG-DEFAULT",
        "event_type": "node_completed", "event_time": utc_now(), "stage": "discover",
        "status": "complete", "message": "a event", "duration_s": 0.1,
        "node_name": None, "execution_key": None, "actor": "system", "state_version": 1,
    })
    real_ctx.persistence.append_trace_event({
        "event_id": "EVT-B1", "run_id": "RUN-B", "engagement_id": "ENG-DEFAULT",
        "event_type": "node_completed", "event_time": utc_now(), "stage": "discover",
        "status": "complete", "message": "b event", "duration_s": 0.1,
        "node_name": None, "execution_key": None, "actor": "system", "state_version": 1,
    })

    layout = platform_trace_page(run_id="RUN-A")
    text = str(layout)
    assert "a event" in text
    assert "b event" not in text


def test_trace_filter_callback_does_not_fire_on_initial_mount():
    """BUG-TRACE-2 (src/trace_page.py)."""
    from conftest import load_app_entry

    entry = load_app_entry()
    matches = [c for c in entry.app._callback_list if c["output"] == "trace-events-body.children"]
    assert matches and all(c["prevent_initial_call"] for c in matches)


# ── /workspace/tne ───────────────────────────────────────────────────────


def test_get_run_payload_and_frames_shares_one_load_state(monkeypatch, real_ctx):
    """P3/P4 perf gap review 2026-09-25 (/workspace/tne cold-load latency
    pass): _load_bundle used to call get_run, then get_run_payload, then
    get_run_frames, each independently loading this run's RunState -- three
    persistence.load_state round trips against the exact same row.
    adapters.get_run_payload_and_frames shares ONE load between the payload
    and frames reads (the third, in _load_bundle's own initial get_run()
    cache check, stays separate on purpose -- see that function's own
    docstring). A bare "queued" run has no bound data source, so
    get_run_frames' own fallback path is expected to raise here -- this
    test only asserts how many times persistence.load_state itself was
    called before that happens, not that frames succeeds."""
    run_id = "RUN-A"
    _make_run(real_ctx, run_id)

    calls: list[str] = []
    original = real_ctx.persistence.load_state

    def _tracked(rid):
        calls.append(rid)
        return original(rid)

    monkeypatch.setattr(real_ctx.persistence, "load_state", _tracked)

    try:
        adapters.get_run_payload_and_frames(run_id)
    except Exception:
        pass

    assert calls == [run_id], (
        f"expected exactly one shared persistence.load_state call, got {len(calls)}: {calls}"
    )
