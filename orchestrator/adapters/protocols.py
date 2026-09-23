from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from orchestrator.state import RunState


class DataSourceAdapter(Protocol):
    """Gives the caller the tested population without exposing pandas vs Spark."""

    def resolve_source_versions(self, table_fqns: list[str]) -> dict[str, str]:
        ...

    def read_population(
        self,
        table_fqn: str,
        *,
        version: str | None = None,
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
