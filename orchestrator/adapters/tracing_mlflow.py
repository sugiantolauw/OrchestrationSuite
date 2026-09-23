"""TracingAdapter (orchestrator/adapters/protocols.py) backed by MLflow --
one MLflow run per pipeline run (CLAUDE.md §2.3: "an MLflow run per pipeline
run"), one NESTED run per node attempt (a "span" in this Protocol's naming;
MLflow OSS's Runs API -- params, tags, a start/end time and a terminal
status, all first-class on a run -- maps onto exactly what a node attempt
needs, with no separate tracing backend to stand up).

CLAUDE.md §2.3 rule 4 / non-negotiable 13: tracing NEVER changes the audit
result and NEVER silently pretends. If `mlflow` cannot be imported, or the
tracking URI/experiment cannot be reached, `available` is False and every
call is a documented no-op returning `""`; the caller
(`orchestrator.pipeline.run_phase`) is responsible for recording ONE
"tracing unavailable" trace_event and proceeding -- this module never raises
out of a tracing call into the pipeline.

Uses `mlflow.tracking.MlflowClient` directly rather than the fluent
`mlflow.start_run`/active-run-stack API: the fluent API's "current active
run" is a process-global (thread-local) stack, and this pipeline runs
several audit runs concurrently on different threads (CLAUDE.md §2.3,
`Semaphore(MAX_CONCURRENT_RUNS)`) -- relying on an implicit active run would
let one thread's span attribute itself to a different thread's run under
any interleaving. `MlflowClient` addresses every run/span by its own id
explicitly, so there is no shared mutable state between threads."""

from __future__ import annotations

import threading
from typing import Any

_DEFAULT_EXPERIMENT_NAME = "orchestrator-audit-runs"


class MLflowTracingAdapter:
    def __init__(self, *, tracking_uri: str | None = None, experiment_name: str = _DEFAULT_EXPERIMENT_NAME):
        self.available = False
        self.unavailable_reason: str | None = None
        self._client = None
        self._experiment_id: str | None = None
        self._run_id_cache: dict[str, str] = {}
        self._lock = threading.Lock()

        try:
            from mlflow.tracking import MlflowClient

            client = MlflowClient(tracking_uri=tracking_uri)
            experiment = client.get_experiment_by_name(experiment_name)
            experiment_id = (
                experiment.experiment_id if experiment is not None else client.create_experiment(experiment_name)
            )
            self._client = client
            self._experiment_id = experiment_id
            self.available = True
        except Exception as exc:  # pragma: no cover - exercised by test_tracing_mlflow's forced-unavailable case
            self.unavailable_reason = f"{type(exc).__name__}: {exc}"

    # ── TracingAdapter protocol ──────────────────────────────────────────────

    def start_run(self, run_id: str, *, params: dict[str, Any] | None = None) -> str:
        """`params` is an extension beyond the Protocol's minimum signature
        (structurally compatible -- every existing caller that passes only
        `run_id` still works): orchestrator.pipeline.run_phase uses it to log
        this run's skill id/version, fingerprint_id and code_revision once,
        at first creation. Idempotent per `run_id` for the lifetime of this
        adapter instance (an in-memory cache) AND across process restarts (a
        tag-keyed MLflow search finds a prior run's own mlflow run_id) -- an
        executor pass resuming a paused run must attach to the SAME MLflow
        run, never start a second one (CLAUDE.md §2.3: "an MLflow run PER
        PIPELINE RUN", not per executor pass)."""
        if not self.available:
            return ""
        with self._lock:
            cached = self._run_id_cache.get(run_id)
            if cached:
                return cached

            existing = self._client.search_runs(
                experiment_ids=[self._experiment_id],
                filter_string=(
                    f"tags.orchestrator_run_id = '{run_id}' and tags.orchestrator_span = 'run'"
                ),
                max_results=1,
            )
            if existing:
                mlflow_run_id = existing[0].info.run_id
            else:
                run = self._client.create_run(
                    self._experiment_id,
                    tags={
                        "orchestrator_run_id": run_id,
                        "orchestrator_span": "run",
                        "mlflow.runName": run_id,
                    },
                    run_name=run_id,
                )
                mlflow_run_id = run.info.run_id
                for key, value in (params or {}).items():
                    if value is None:
                        continue
                    self._client.log_param(mlflow_run_id, key, value)
            self._run_id_cache[run_id] = mlflow_run_id
            return mlflow_run_id

    def start_span(self, *, run_id: str, node_name: str) -> str:
        if not self.available:
            return ""
        parent_mlflow_run_id = self.start_run(run_id)
        if not parent_mlflow_run_id:
            return ""
        span = self._client.create_run(
            self._experiment_id,
            tags={
                "orchestrator_run_id": run_id,
                "orchestrator_node": node_name,
                "orchestrator_span": "node",
                "mlflow.parentRunId": parent_mlflow_run_id,
                "mlflow.runName": node_name,
            },
            run_name=node_name,
        )
        self._client.log_param(span.info.run_id, "node_name", node_name)
        return span.info.run_id

    def end_span(self, span_id: str, *, outcome: str, attributes: dict[str, Any] | None = None) -> None:
        if not self.available or not span_id:
            return
        for key, value in (attributes or {}).items():
            if value is None:
                continue
            self._client.log_param(span_id, key, value)
        self._client.set_tag(span_id, "orchestrator_outcome", outcome)
        status = "FINISHED" if outcome == "succeeded" else "FAILED"
        self._client.set_terminated(span_id, status=status)


