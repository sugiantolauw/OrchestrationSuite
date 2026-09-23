from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from orchestrator.state import RunState


class DataSourceAdapter(Protocol):
    """Gives the caller the tested population without exposing pandas vs Spark."""

    def resolve_source_versions(self, table_fqns: list[str]) -> dict[str, str]:
        ...

    def resolve_version(self, source: str) -> str:
        """Resolve a single source's version (e.g. via DESCRIBE HISTORY). Callers must
        resolve_version() first and pass the *same* version into read_population's
        VERSION AS OF -- resolving after the read would leave a TOCTOU gap between what
        was read and what provenance records were captured (CLAUDE.md §4.1)."""
        ...

    def read_population(
        self,
        source: str,
        *,
        version: int | str,
        columns: list[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> Any:
        ...

    def aggregate(
        self,
        table_fqn: str,
        *,
        version: str | None = None,
        group_by: list[str],
        aggregations: dict[str, str],
        filters: dict[str, Any] | None = None,
    ) -> Any:
        ...

    def row_count(self, table_fqn: str, *, version: str | None = None) -> int:
        ...


class ModelClient(Protocol):
    def chat(
        self,
        *,
        endpoint: str,
        messages: list[dict],
        response_format: dict | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        ...


class PersistenceAdapter(Protocol):
    def migrate(self) -> list[str]:
        ...

    def create_run(self, state: RunState, fingerprint: dict) -> RunState:
        ...

    def load_state(self, run_id: str) -> RunState:
        ...

    def save_state(self, state: RunState) -> RunState:
        ...

    def get_fingerprint(self, fingerprint_id: str) -> dict:
        ...

    def begin_node_attempt(
        self,
        *,
        run_id: str,
        phase: str,
        phase_epoch: int = 1,
        node_index: int,
        node_name: str,
        state_version_before: int,
        now: str,
    ) -> dict:
        ...

    def complete_node_attempt(
        self,
        execution_key: str,
        *,
        outcome: str,
        now: str,
        state_version_after: int | None = None,
        result_state_json: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        ...

    def list_node_attempts(self, run_id: str) -> list[dict]:
        ...

    def append_trace_event(self, event: dict) -> None:
        ...

    def list_trace_events(self, run_id: str | None = None) -> list[dict]:
        ...

    def list_runs(self, filters: dict | None = None) -> list[dict]:
        ...

    def find_runs(self, statuses: list[str]) -> list[str]:
        ...

    def close_open_attempts(
        self, run_id: str, *, outcome: str, now: str, error_detail: str | None = None
    ) -> None:
        ...

    def repair_projections(self) -> int:
        """Rewrites any `runs` row whose state_version lags its `run_state` row (a save
        whose projection update failed and was swallowed, CLAUDE.md §9C/B4). Returns the
        number of rows repaired. Called at App start alongside the reaper."""
        ...

    def get_engagement(self, engagement_id: str) -> dict | None:
        ...

    def list_engagements(self) -> list[dict]:
        ...

    def record_skill_version(
        self,
        *,
        skill_id: str,
        version: str,
        content_hash: str,
        content: dict,
        created_by: str,
        now: str,
        status: str = "draft",
    ) -> dict:
        ...

    def get_skill_version(self, skill_id: str, version: str) -> dict | None:
        ...

    def list_skill_versions(self, skill_id: str) -> list[dict]:
        ...

    def upsert_risks(self, risks: list[dict], *, now: str) -> None:
        ...

    def upsert_controls(self, controls: list[dict], *, now: str) -> None:
        ...

    def list_risks(self, engagement_id: str | None = None) -> list[dict]:
        ...

    def list_controls(self, engagement_id: str | None = None) -> list[dict]:
        ...

    def write_findings(
        self,
        run_id: str,
        findings: list[dict],
        *,
        engagement_id: str | None,
        skill_id: str | None,
        skill_version: str | None,
        now: str,
    ) -> list[dict]:
        ...

    def list_findings(self, run_id: str) -> list[dict]:
        ...

    def set_finding_review_state(self, finding_id: str, *, to_state: str, actor: str, now: str) -> dict:
        ...

    # ── P3 run outputs (CLAUDE.md §4.2, §9C) ────────────────────────────────

    def write_flagged_rows(self, run_id: str, rows: list[dict]) -> None:
        """Idempotent replace-per-run: `rows` (each {source, row_key, flag,
        group_id}) becomes this run's entire flagged_rows set -- a re-run of the
        node that produced them overwrites, never appends duplicates."""
        ...

    def list_flagged_rows(self, run_id: str, flag: str | None = None) -> list[dict]:
        ...

    def write_run_metrics(self, run_id: str, metrics: list[dict]) -> None:
        """Idempotent replace-per-run: `metrics` (each {metric_name, value,
        unit, source_ref, test_id}) becomes this run's entire run_metrics set."""
        ...

    def get_run_metrics(self, run_id: str) -> dict[str, dict]:
        ...

    def write_issues_for_findings(
        self, run_id: str, findings: list[dict], *, engagement_id: str | None, now: str
    ) -> list[dict]:
        """One issue per finding (issue_id = f'ISS-{finding_id}'), status 'draft'.
        Idempotent: a repeat call for an issue_id that already exists leaves it
        untouched (its status may have moved on since)."""
        ...

    def write_management_actions(self, run_id: str, actions: list[dict], *, now: str) -> None:
        """Idempotent replace-per-run, same shape as write_flagged_rows."""
        ...

    def list_management_actions(self, filters: dict | None = None) -> list[dict]:
        ...

    def record_export(
        self, run_id: str, kind: str, *, path: str, sha256: str, created_by: str, now: str
    ) -> dict:
        ...

    def list_exports(self, run_id: str) -> list[dict]:
        ...

    def acquire_lease(self, run_id: str, worker_id: str, *, ttl_s: float, now: str) -> bool:
        ...

    def renew_lease(self, run_id: str, worker_id: str, *, ttl_s: float, now: str) -> bool:
        ...

    def release_lease(self, run_id: str, worker_id: str) -> None:
        ...

    def expired_leases(self, now: str) -> list[str]:
        ...


class PromptRepository(Protocol):
    def get_prompt(self, task: str, *, skill_id: str | None = None) -> str:
        ...

    def prompt_template_version(self, task: str, *, skill_id: str | None = None) -> str:
        ...


class TracingAdapter(Protocol):
    def start_run(self, run_id: str) -> str:
        ...

    def start_span(self, *, run_id: str, node_name: str) -> str:
        ...

    def end_span(self, span_id: str, *, outcome: str, attributes: dict | None = None) -> None:
        ...


class ExportStorageAdapter(Protocol):
    def write(self, path: str, content: bytes) -> str:
        ...

    def read(self, path: str) -> bytes:
        ...

    def exists(self, path: str) -> bool:
        ...


@dataclass(frozen=True)
class Document:
    doc_id: str
    title: str
    source: str
    as_of: date | None
    content: str
    citation_ref: str


class KnowledgeSourceAdapter(Protocol):
    def search(self, query: str, *, as_of: date | None) -> list[Document]:
        ...

    def fetch(self, doc_id: str) -> Document:
        ...


class Executor(Protocol):
    def start(self, run_id: str, phase: str) -> None:
        ...
