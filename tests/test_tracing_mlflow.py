"""CLAUDE.md §2.3 / P2/P3 gate review item 4: the TracingAdapter wiring in
orchestrator.pipeline.run_phase (a fake tracer, exercising exactly what the
pipeline calls and with what arguments/ordering) and orchestrator.adapters.
tracing_mlflow.MLflowTracingAdapter itself against a real local file-store
MLflow tracking URI in a tmp dir. Tracing must never change the audit result
and never silently pretend (non-negotiable 13): every "unavailable" path
here asserts the run still completes normally and exactly one
`tracing_unavailable` trace_event is recorded."""

from __future__ import annotations

import dataclasses

from orchestrator import runs
from orchestrator.pipeline import run_phase


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
    # Noted exactly once, despite start_run AND two start_span calls all raising.
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
