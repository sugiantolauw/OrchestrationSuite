"""CLAUDE.md §2.3 / P2/P3 gate review item 4: the TracingAdapter wiring in
orchestrator.pipeline.run_phase (a fake tracer, exercising exactly what the
pipeline calls and with what arguments/ordering) and orchestrator.adapters.
tracing_mlflow.MLflowTracingAdapter itself against a real local file-store
MLflow tracking URI in a tmp dir. Tracing must never change the audit result
and never silently pretend (non-negotiable 13): every "unavailable" path
here asserts the run still completes normally and exactly one
`tracing_unavailable` trace_event is recorded.

P3 gap-audit review (item 4, "the MLflow parent run never terminates"):
also covers end_run -- called at every RunState terminal transition
(completed -> FINISHED, failed -> FAILED, interrupted -> KILLED via the
executor's reaper) and at each HITL pause gate (awaiting_confirmation /
awaiting_signoff -> FINISHED), and that a resumed run continues logging
into the SAME mlflow parent run rather than opening a second one."""

from __future__ import annotations

import dataclasses

from orchestrator import runs
from orchestrator.pipeline import run_phase
from orchestrator.status import transition


def _fingerprint(fp_id="FP-TR", code_revision="rev-abc"):
    return dict(
        fingerprint_id=fp_id, source_table_versions="{}", uploaded_file_hashes="{}",
        reference_data_hashes="{}", skill_content_hash=None, code_revision=code_revision,
        dependency_lock_hash="dep1", runtime_config_hash="rc1", endpoint_config="{}",
        prompt_template_version="none", created_at="2026-01-01T00:00:00.000000Z",
    )


def _create(persistence, clock, fp):
    return runs.create_run(
        persistence, run_kind="fieldwork", engagement_id="ENG-DEFAULT", skill_id="SKILL-X",
        skill_version="1.0.0", mode="playbook", audit_period=("2026-01-01", "2026-01-31"),
        objective="t", run_owner="alice", fingerprint=fp, now=clock(),
    )


def _two_node_setup():
    def node_a(ctx, state):
        return dataclasses.replace(state, events=state.events + [{"node": "a"}])

    def node_b(ctx, state):
        return dataclasses.replace(state, events=state.events + [{"node": "b"}], findings=[{"id": "F1"}])

    return {"fieldwork": {"plan": [("discover", node_a), ("profile", node_b)], "execute": [], "export": []}}


# ── fake tracer: exercises the pipeline's own wiring ────────────────────────────


class _FakeTracer:
    available = True
    unavailable_reason = None

    def __init__(self):
        self.run_calls: list[tuple] = []
        self.span_calls: list[tuple] = []
        self.end_calls: list[tuple] = []
        self.run_end_calls: list[tuple] = []
        self._next_id = 0

    def start_run(self, run_id, **kwargs):
        self.run_calls.append((run_id, kwargs.get("params")))
        return "fake-run-1"

    def start_span(self, *, run_id, node_name):
        self._next_id += 1
        span_id = f"fake-span-{self._next_id}"
        self.span_calls.append((run_id, node_name, span_id))
        return span_id

    def end_span(self, span_id, *, outcome, attributes=None):
        self.end_calls.append((span_id, outcome, attributes))

    def end_run(self, run_id, *, status):
        self.run_end_calls.append((run_id, status))


def test_fake_tracer_gets_one_run_and_one_span_per_node(local_persistence, clock):
    persistence = local_persistence
    fp = _fingerprint()
    state = _create(persistence, clock, fp)
    tracer = _FakeTracer()

    final = run_phase(
        persistence, state.run_id, nodes_for=_two_node_setup(), clock=clock,
        current_fingerprint=fp, tracing=tracer,
    )
    assert final.status == "awaiting_confirmation"

    # One MLflow run per pipeline run (CLAUDE.md §2.3), with skill/fingerprint/
    # code-revision params -- never per node.
    assert len(tracer.run_calls) == 1
    run_id_called, params = tracer.run_calls[0]
    assert run_id_called == state.run_id
    assert params["skill_id"] == "SKILL-X"
    assert params["skill_version"] == "1.0.0"
    assert params["fingerprint_id"] == state.fingerprint_id
    assert params["code_revision"] == "rev-abc"

    # One span per node attempt, in order, each ended exactly once as succeeded.
    assert [c[1] for c in tracer.span_calls] == ["discover", "profile"]
    assert [c[0] for c in tracer.end_calls] == [s[2] for s in tracer.span_calls]
    assert [c[1] for c in tracer.end_calls] == ["succeeded", "succeeded"]
    for _, _, attrs in tracer.end_calls:
        assert attrs["attempt_number"] == 1
        assert isinstance(attrs["duration_s"], float)

    # A HITL pause gate (awaiting_confirmation) ends the parent run FINISHED --
    # this executor pass genuinely finished, even though the RunState itself
    # is paused, not terminal (P3 gap-audit review).
    assert tracer.run_end_calls == [(state.run_id, "FINISHED")]


def test_fake_tracer_records_failed_span_on_node_failure(local_persistence, clock):
    persistence = local_persistence
    fp = _fingerprint()
    state = _create(persistence, clock, fp)
    tracer = _FakeTracer()

    def rogue(ctx, state):
        raise ValueError("boom")

    nodes_for = {"fieldwork": {"plan": [("discover", rogue)], "execute": [], "export": []}}
    final = run_phase(
        persistence, state.run_id, nodes_for=nodes_for, clock=clock,
        current_fingerprint=fp, tracing=tracer,
    )
    assert final.status == "failed"
    assert len(tracer.end_calls) == 1
    _, outcome, attrs = tracer.end_calls[0]
    assert outcome == "failed"
    assert "boom" in attrs["error"]

    # The parent run also reaches a terminal state -- FAILED, matching the
    # RunState transition (P3 gap-audit review).
    assert tracer.run_end_calls == [(state.run_id, "FAILED")]


def test_fake_tracer_ends_the_parent_run_at_every_pause_gate_and_on_completion(local_persistence, clock):
    """Drives a full plan -> execute -> export journey through TWO run_phase
    calls (a real executor pass never runs more than one phase past a HITL
    gate) -- the parent run must end FINISHED at BOTH pause gates
    (awaiting_confirmation is skipped here via auto_confirm_plan, so only
    awaiting_signoff is exercised as a gate) and again FINISHED once the run
    reaches `completed`, never left RUNNING across any of them."""
    persistence = local_persistence
    fp = _fingerprint()
    state = runs.create_run(
        persistence, run_kind="fieldwork", engagement_id="ENG-DEFAULT", skill_id="SKILL-X",
        skill_version="1.0.0", mode="playbook", audit_period=("2026-01-01", "2026-01-31"),
        objective="t", run_owner="alice", fingerprint=fp, now=clock(),
        options={"auto_confirm_plan": True},
    )
    tracer = _FakeTracer()

    def node_a(ctx, state):
        return dataclasses.replace(state, events=state.events + [{"node": "a"}])

    def node_c(ctx, state):
        return dataclasses.replace(state, events=state.events + [{"node": "c"}])

    nodes_for = {"fieldwork": {"plan": [("discover", node_a)], "execute": [], "export": [("export", node_c)]}}

    first = run_phase(
        persistence, state.run_id, nodes_for=nodes_for, clock=clock,
        current_fingerprint=fp, tracing=tracer,
    )
    assert first.status == "awaiting_signoff"
    assert tracer.run_end_calls == [(state.run_id, "FINISHED")]

    # Simulate sign_off: the same phase-transition orchestrator.runs.sign_off
    # performs (signoff set, then execute -> export via the "queued" gate),
    # without its candidate-decision bookkeeping this test does not exercise.
    presigned = dataclasses.replace(first, signoff={"approver": "bob", "timestamp": clock()})
    signed = transition(presigned, "queued", now=clock(), phase="export")
    persistence.save_state(signed)

    second = run_phase(
        persistence, state.run_id, nodes_for=nodes_for, clock=clock,
        current_fingerprint=fp, tracing=tracer,
    )
    assert second.status == "completed"
    # ONE mlflow run throughout (start_run called for the SAME run_id both
    # passes -- the fake always returns "fake-run-1", proving no caller here
    # ever asked for a second one), ended FINISHED at the signoff gate and
    # FINISHED again on completion.
    assert {r for r, _ in tracer.run_calls} == {state.run_id}
    assert tracer.run_end_calls == [
        (state.run_id, "FINISHED"),  # awaiting_signoff
        (state.run_id, "FINISHED"),  # completed
    ]


def test_no_tracer_passed_defaults_to_null_tracing_no_spurious_events(local_persistence, clock):
    persistence = local_persistence
    fp = _fingerprint()
    state = _create(persistence, clock, fp)
    final = run_phase(persistence, state.run_id, nodes_for=_two_node_setup(), clock=clock, current_fingerprint=fp)
    assert final.status == "awaiting_confirmation"
    events = persistence.list_trace_events(state.run_id)
    assert not any(e["event_type"] == "tracing_unavailable" for e in events)


# ── tracing failure must never change the audit result ──────────────────────────


class _RaisingTracer:
    available = True
    unavailable_reason = None

    def start_run(self, run_id, **kwargs):
        raise RuntimeError("mlflow down")

    def start_span(self, *, run_id, node_name):
        raise RuntimeError("mlflow down")

    def end_span(self, span_id, *, outcome, attributes=None):
        raise RuntimeError("mlflow down")

    def end_run(self, run_id, *, status):
        raise RuntimeError("mlflow down")


def test_tracing_exception_is_caught_noted_once_never_fails_the_run(local_persistence, clock):
    persistence = local_persistence
    fp = _fingerprint()
    state = _create(persistence, clock, fp)
    tracer = _RaisingTracer()

    final = run_phase(
        persistence, state.run_id, nodes_for=_two_node_setup(), clock=clock,
        current_fingerprint=fp, tracing=tracer,
    )
    assert final.status == "awaiting_confirmation"  # the audit result is unaffected
    assert [e["node"] for e in final.events] == ["a", "b"]

    events = persistence.list_trace_events(state.run_id)
    unavailable = [e for e in events if e["event_type"] == "tracing_unavailable"]
    # Noted exactly once, despite start_run AND two start_span calls all raising
    # -- and end_run raising too (the pause-gate call this run also makes)
    # must not add a second note, or fail the run either (CLAUDE.md §2.3 rule
    # 4: tracing never changes the audit result).
    assert len(unavailable) == 1
    assert "mlflow down" in unavailable[0]["message"]


class _UnavailableTracer:
    available = False
    unavailable_reason = "mlflow package not installed"

    def start_run(self, run_id, **kwargs):
        return ""

    def start_span(self, *, run_id, node_name):
        return ""

    def end_span(self, span_id, *, outcome, attributes=None):
        return None

    def end_run(self, run_id, *, status):
        return None


def test_reported_unavailable_tracer_is_noted_once_and_run_proceeds(local_persistence, clock):
    persistence = local_persistence
    fp = _fingerprint()
    state = _create(persistence, clock, fp)
    tracer = _UnavailableTracer()

    final = run_phase(
        persistence, state.run_id, nodes_for=_two_node_setup(), clock=clock,
        current_fingerprint=fp, tracing=tracer,
    )
    assert final.status == "awaiting_confirmation"

    events = persistence.list_trace_events(state.run_id)
    unavailable = [e for e in events if e["event_type"] == "tracing_unavailable"]
    assert len(unavailable) == 1
    assert "mlflow package not installed" in unavailable[0]["message"]


# ── real MLflowTracingAdapter against a local file-store tracking URI ───────────


def test_mlflow_adapter_creates_run_and_nested_spans(tmp_path, monkeypatch):
    from orchestrator.adapters.tracing_mlflow import MLflowTracingAdapter

    # This installed mlflow (3.16.1) deprecates the filesystem tracking
    # backend in favour of a DB backend and refuses it unless explicitly
    # opted back into -- the local file-store URI this test (and P2/P3 gate
    # review item 4) asks for is exactly that filesystem backend.
    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
    tracking_uri = f"file://{tmp_path / 'mlruns'}"
    adapter = MLflowTracingAdapter(tracking_uri=tracking_uri, experiment_name="test-exp-1")
    assert adapter.available, adapter.unavailable_reason

    mlflow_run_id = adapter.start_run("RUN-123", params={"skill_id": "SKILL-001", "code_revision": "abc123"})
    assert mlflow_run_id

    # Idempotent per run_id: a second call on the SAME instance (in-memory
    # cache) and a FRESH instance forced to search cold both resolve to the
    # SAME mlflow run -- "one MLflow run per pipeline run" must hold across
    # separate executor passes, not just within one process.
    again = adapter.start_run("RUN-123")
    assert again == mlflow_run_id

    cold = MLflowTracingAdapter(tracking_uri=tracking_uri, experiment_name="test-exp-1")
    resumed = cold.start_run("RUN-123")
    assert resumed == mlflow_run_id

    span_id = adapter.start_span(run_id="RUN-123", node_name="execute")
    assert span_id and span_id != mlflow_run_id
    adapter.end_span(span_id, outcome="succeeded", attributes={"duration_s": 1.5, "attempt_number": 1})

    from mlflow.tracking import MlflowClient

    client = MlflowClient(tracking_uri=tracking_uri)
    run = client.get_run(mlflow_run_id)
    assert run.data.params["skill_id"] == "SKILL-001"
    assert run.data.params["code_revision"] == "abc123"

    span = client.get_run(span_id)
    assert span.data.tags["mlflow.parentRunId"] == mlflow_run_id
    assert span.data.tags["orchestrator_node"] == "execute"
    assert span.data.tags["orchestrator_outcome"] == "succeeded"
    assert span.info.status == "FINISHED"
    assert span.data.params["duration_s"] == "1.5"


def test_mlflow_adapter_end_run_sets_terminal_status_and_is_idempotent(tmp_path, monkeypatch):
    """P3 gap-audit review, item 4: end_run must actually terminate the
    parent run (previously nothing did -- it stayed RUNNING forever), and
    calling it again with a different status (e.g. FINISHED at a pause gate,
    then the run's real final outcome later) must update the SAME run's
    status rather than erroring or creating a second one."""
    from mlflow.tracking import MlflowClient

    from orchestrator.adapters.tracing_mlflow import MLflowTracingAdapter

    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
    tracking_uri = f"file://{tmp_path / 'mlruns'}"
    adapter = MLflowTracingAdapter(tracking_uri=tracking_uri, experiment_name="test-exp-end-run")
    client = MlflowClient(tracking_uri=tracking_uri)

    mlflow_run_id = adapter.start_run("RUN-END-1", params={"skill_id": "SKILL-X"})
    run = client.get_run(mlflow_run_id)
    assert run.info.status == "RUNNING"

    adapter.end_run("RUN-END-1", status="FINISHED")
    assert client.get_run(mlflow_run_id).info.status == "FINISHED"

    # A later, different terminal status (the run's true outcome, found out
    # after a pause-gate FINISHED) updates the SAME mlflow run in place --
    # never a second one.
    adapter.end_run("RUN-END-1", status="FAILED")
    assert client.get_run(mlflow_run_id).info.status == "FAILED"

    existing = client.search_runs(
        experiment_ids=[adapter._experiment_id],
        filter_string="tags.orchestrator_run_id = 'RUN-END-1' and tags.orchestrator_span = 'run'",
    )
    assert len(existing) == 1, "end_run must never create a second parent run"


def test_mlflow_adapter_end_run_without_a_prior_start_run_still_resolves_the_same_run(tmp_path, monkeypatch):
    """A fresh adapter instance (a different executor pass/process, e.g. the
    reaper marking a run interrupted) that never called start_run itself
    must still terminate the SAME existing mlflow run via the tag-keyed
    lookup, not create a fresh one just to immediately end it."""
    from mlflow.tracking import MlflowClient

    from orchestrator.adapters.tracing_mlflow import MLflowTracingAdapter

    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
    tracking_uri = f"file://{tmp_path / 'mlruns'}"
    creator = MLflowTracingAdapter(tracking_uri=tracking_uri, experiment_name="test-exp-end-run-cold")
    mlflow_run_id = creator.start_run("RUN-END-COLD")

    reaper_side = MLflowTracingAdapter(tracking_uri=tracking_uri, experiment_name="test-exp-end-run-cold")
    reaper_side.end_run("RUN-END-COLD", status="KILLED")

    client = MlflowClient(tracking_uri=tracking_uri)
    assert client.get_run(mlflow_run_id).info.status == "KILLED"
    existing = client.search_runs(
        experiment_ids=[creator._experiment_id],
        filter_string="tags.orchestrator_run_id = 'RUN-END-COLD' and tags.orchestrator_span = 'run'",
    )
    assert len(existing) == 1


def test_mlflow_adapter_marks_failed_span(tmp_path, monkeypatch):
    from mlflow.tracking import MlflowClient

    from orchestrator.adapters.tracing_mlflow import MLflowTracingAdapter

    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
    tracking_uri = f"file://{tmp_path / 'mlruns'}"
    adapter = MLflowTracingAdapter(tracking_uri=tracking_uri, experiment_name="test-exp-2")
    span_id = adapter.start_span(run_id="RUN-456", node_name="execute")
    adapter.end_span(span_id, outcome="failed", attributes={"error": "boom"})

    client = MlflowClient(tracking_uri=tracking_uri)
    span = client.get_run(span_id)
    assert span.info.status == "FAILED"
    assert span.data.tags["orchestrator_outcome"] == "failed"


def test_run_phase_with_real_adapter_resumes_into_the_same_parent_run_and_ends_finished(
    local_persistence, clock, tmp_path, monkeypatch
):
    """End-to-end through run_phase and a REAL MLflowTracingAdapter (not the
    fake): a run paused at awaiting_signoff and then resumed into export
    logs its second-pass span as a CHILD of the SAME parent run the first
    pass created (never a second parent), and the parent reaches FINISHED
    once the run completes -- proving "resume re-opens or continues
    correctly" (P3 gap-audit review, item 4) against the real adapter, not
    just the fake's bookkeeping."""
    from mlflow.tracking import MlflowClient

    from orchestrator.adapters.tracing_mlflow import MLflowTracingAdapter

    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
    tracking_uri = f"file://{tmp_path / 'mlruns'}"
    adapter = MLflowTracingAdapter(tracking_uri=tracking_uri, experiment_name="test-exp-resume")

    persistence = local_persistence
    fp = _fingerprint(fp_id="FP-RESUME", code_revision="rev-resume")
    state = runs.create_run(
        persistence, run_kind="fieldwork", engagement_id="ENG-DEFAULT", skill_id="SKILL-X",
        skill_version="1.0.0", mode="playbook", audit_period=("2026-01-01", "2026-01-31"),
        objective="t", run_owner="alice", fingerprint=fp, now=clock(),
        options={"auto_confirm_plan": True},
    )

    def node_a(ctx, state):
        return dataclasses.replace(state, events=state.events + [{"node": "a"}])

    def node_c(ctx, state):
        return dataclasses.replace(state, events=state.events + [{"node": "c"}])

    nodes_for = {"fieldwork": {"plan": [("discover", node_a)], "execute": [], "export": [("export", node_c)]}}

    first = run_phase(
        persistence, state.run_id, nodes_for=nodes_for, clock=clock,
        current_fingerprint=fp, tracing=adapter,
    )
    assert first.status == "awaiting_signoff"

    client = MlflowClient(tracking_uri=tracking_uri)
    parent_id = adapter._run_id_cache[state.run_id]
    assert client.get_run(parent_id).info.status == "FINISHED"

    # A SEPARATE adapter instance, as a resumed executor pass on a different
    # worker/process would construct -- must find the SAME parent via the
    # tag-keyed lookup, not create a second one.
    cold_adapter = MLflowTracingAdapter(tracking_uri=tracking_uri, experiment_name="test-exp-resume")
    presigned = dataclasses.replace(first, signoff={"approver": "bob", "timestamp": clock()})
    signed = transition(presigned, "queued", now=clock(), phase="export")
    persistence.save_state(signed)

    second = run_phase(
        persistence, state.run_id, nodes_for=nodes_for, clock=clock,
        current_fingerprint=fp, tracing=cold_adapter,
    )
    assert second.status == "completed"
    assert cold_adapter._run_id_cache[state.run_id] == parent_id
    assert client.get_run(parent_id).info.status == "FINISHED"

    existing = client.search_runs(
        experiment_ids=[adapter._experiment_id],
        filter_string=f"tags.orchestrator_run_id = '{state.run_id}' and tags.orchestrator_span = 'run'",
    )
    assert len(existing) == 1, "resume must never create a second parent run"

    # Both nodes' spans are children of the SAME parent, across both passes.
    node_names = {
        r.data.tags.get("orchestrator_node")
        for r in client.search_runs(
            experiment_ids=[adapter._experiment_id],
            filter_string=f"tags.mlflow.parentRunId = '{parent_id}'",
        )
    }
    assert node_names == {"discover", "export"}


def test_unset_tracking_uri_is_unavailable_and_never_touches_mlflow(tmp_path, monkeypatch):
    # Regression: an unset tracking_uri must NOT fall through to mlflow's own
    # default resolution, which silently creates a real local tracking store
    # (./mlflow.db / ./mlruns) the first time anything touches it -- found
    # live when every test constructing an AppContext without
    # MLFLOW_TRACKING_URI set was writing to a shared repo-root mlflow.db.
    from orchestrator.adapters.tracing_mlflow import MLflowTracingAdapter

    monkeypatch.chdir(tmp_path)
    adapter = MLflowTracingAdapter(tracking_uri=None)
    assert not adapter.available
    assert "not configured" in adapter.unavailable_reason or "unset" in adapter.unavailable_reason
    assert adapter.start_run("RUN-1") == ""
    assert adapter.start_span(run_id="RUN-1", node_name="x") == ""
    adapter.end_run("RUN-1", status="FINISHED")  # no-op, must not raise or touch mlflow
    assert not (tmp_path / "mlflow.db").exists()
    assert not (tmp_path / "mlruns").exists()


def test_mlflow_adapter_unavailable_is_a_documented_no_op(monkeypatch):
    import orchestrator.adapters.tracing_mlflow as mod

    class _BrokenClient:
        def __init__(self, tracking_uri=None):
            raise RuntimeError("cannot connect")

    monkeypatch.setattr("mlflow.tracking.MlflowClient", _BrokenClient)
    adapter = mod.MLflowTracingAdapter(tracking_uri="file:///nonexistent")
    assert not adapter.available
    assert "cannot connect" in adapter.unavailable_reason
    assert adapter.start_run("RUN-1") == ""
    assert adapter.start_span(run_id="RUN-1", node_name="x") == ""
    adapter.end_span("", outcome="succeeded")  # no-op, must not raise
    adapter.end_run("RUN-1", status="FINISHED")  # no-op, must not raise


def test_build_app_context_passes_mlflow_experiment_path_through(monkeypatch, tmp_path):
    """MLflow-on-the-platform item (CLAUDE.md P2/P3 gate review): the
    experiment name orchestrator.service.build_app_context constructs
    MLflowTracingAdapter with must come from MLFLOW_EXPERIMENT_PATH when
    set -- never left at the adapter's own generic, non-absolute-path
    default, which Databricks-managed tracking rejects."""
    import orchestrator.adapters.tracing_mlflow as tracing_mod
    from orchestrator import service

    captured = {}
    real_init = tracing_mod.MLflowTracingAdapter.__init__

    def _spy_init(self, *, tracking_uri=None, experiment_name=tracing_mod._DEFAULT_EXPERIMENT_NAME):
        captured["experiment_name"] = experiment_name
        captured["tracking_uri"] = tracking_uri
        return real_init(self, tracking_uri=tracking_uri, experiment_name=experiment_name)

    monkeypatch.setattr(tracing_mod.MLflowTracingAdapter, "__init__", _spy_init)

    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(tmp_path),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
        "MLFLOW_TRACKING_URI": None,  # unset -> unavailable, but the ctor is still called with our kwargs
        "MLFLOW_EXPERIMENT_PATH": "/Shared/ai-audit-analyst-audit-runs",
    }
    env = {k: v for k, v in env.items() if v is not None}
    service.build_app_context(env)

    assert captured["experiment_name"] == "/Shared/ai-audit-analyst-audit-runs"


def test_build_app_context_uses_adapter_default_when_experiment_path_unset(monkeypatch, tmp_path):
    import orchestrator.adapters.tracing_mlflow as tracing_mod
    from orchestrator import service

    captured = {}
    real_init = tracing_mod.MLflowTracingAdapter.__init__

    def _spy_init(self, *, tracking_uri=None, experiment_name=tracing_mod._DEFAULT_EXPERIMENT_NAME):
        captured["experiment_name"] = experiment_name
        return real_init(self, tracking_uri=tracking_uri, experiment_name=experiment_name)

    monkeypatch.setattr(tracing_mod.MLflowTracingAdapter, "__init__", _spy_init)

    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(tmp_path),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
    }
    service.build_app_context(env)

    assert captured["experiment_name"] == tracing_mod._DEFAULT_EXPERIMENT_NAME
