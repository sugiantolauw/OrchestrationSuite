from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, fields
from datetime import date
from typing import Any, Literal

from orchestrator.errors import NotJsonSafe

RunKind = Literal["fieldwork", "sensing", "assessment", "planning"]
Mode = Literal["playbook", "explorer"]
Phase = Literal["plan", "execute", "export"]
Status = Literal[
    "queued",
    "running",
    "awaiting_confirmation",
    "awaiting_signoff",
    "completed",
    "failed",
    "interrupted",
]

RUN_KINDS: tuple[str, ...] = ("fieldwork", "sensing", "assessment", "planning")
MODES: tuple[str, ...] = ("playbook", "explorer")
PHASES: tuple[str, ...] = ("plan", "execute", "export")
STATUSES: tuple[str, ...] = (
    "queued",
    "running",
    "awaiting_confirmation",
    "awaiting_signoff",
    "completed",
    "failed",
    "interrupted",
)
ENGAGEMENT_SCOPED_KINDS: tuple[str, ...] = ("fieldwork", "assessment", "planning")


@dataclass(frozen=True, kw_only=True)
class RunState:
    run_id: str
    run_kind: RunKind
    engagement_id: str | None = None
    skill_id: str | None = None
    skill_version: str | None = None
    mode: Mode
    phase: Phase
    next_node_index: int = 0
    audit_period: tuple[str, str]
    objective: str
    business_unit: str | None = None
    materiality: float | None = None
    options: dict = field(default_factory=dict)

    # identity and provenance
    run_owner: str
    fingerprint_id: str
    state_version: int = 0

    # timestamps
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    last_state_change_at: str

    # node tracking
    current_node_attempt_id: str | None = None

    data_assets: list[dict] = field(default_factory=list)
    uploaded_files: list[dict] = field(default_factory=list)
    profile_result: dict | None = None
    plan: dict | None = None
    plan_confirmed: bool = False
    plan_edits: list[dict] = field(default_factory=list)

    # results — REFS AND SCALARS ONLY, never DataFrames
    test_results: list[dict] = field(default_factory=list)
    flagged_table: str | None = None
    reconciliation: dict | None = None
    exceptions: list[dict] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    management_actions: list[dict] = field(default_factory=list)
    exports: dict = field(default_factory=dict)
    signoff: dict | None = None
    status_reason: str | None = None

    # LLM narration — never contains raw numbers the model invented
    profile_narrative: str | None = None
    plan_rationale: dict[str, str] = field(default_factory=dict)
    classification_reasoning: dict[str, str] = field(default_factory=dict)
    finding_narratives: dict[str, str] = field(default_factory=dict)
    priority_rationale: dict[str, str] = field(default_factory=dict)
    remediation_drafts: dict[str, str] = field(default_factory=dict)
    exec_summary: str | None = None
    chart_captions: dict[str, str] = field(default_factory=dict)

    events: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    status: Status


def _walk(value: Any, path: str) -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NotJsonSafe(f"{path}: non-finite float {value!r}")
        return
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _walk(item, f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise NotJsonSafe(f"{path}: dict key {k!r} is not a str")
            _walk(v, f"{path}.{k}")
        return
    raise NotJsonSafe(f"{path}: value of type {type(value).__name__} is not JSON-safe")


def assert_json_safe(state: RunState) -> None:
    for f in fields(state):
        _walk(getattr(state, f.name), f.name)


def _to_jsonable(state: RunState) -> dict:
    data = asdict(state)
    data["audit_period"] = list(data["audit_period"])
    return data


def to_json(state: RunState) -> str:
    assert_json_safe(state)
    return json.dumps(_to_jsonable(state), sort_keys=True, separators=(",", ":"))


def from_json(payload: str) -> RunState:
    data = json.loads(payload)
    data["audit_period"] = tuple(data["audit_period"])
    return RunState(**data)


def validate(state: RunState) -> None:
    if state.run_kind not in RUN_KINDS:
        raise ValueError(f"run_kind: invalid value {state.run_kind!r}")
    if state.mode not in MODES:
        raise ValueError(f"mode: invalid value {state.mode!r}")
    if state.phase not in PHASES:
        raise ValueError(f"phase: invalid value {state.phase!r}")
    if state.status not in STATUSES:
        raise ValueError(f"status: invalid value {state.status!r}")

    if len(state.audit_period) != 2:
        raise ValueError("audit_period: must have exactly two ISO dates")
    start_s, end_s = state.audit_period
    try:
        start = date.fromisoformat(start_s)
        end = date.fromisoformat(end_s)
    except ValueError as exc:
        raise ValueError(f"audit_period: not valid ISO dates: {exc}") from exc
    if start > end:
        raise ValueError(f"audit_period: start {start_s} is after end {end_s}")

    if not state.run_owner:
        raise ValueError("run_owner: must be non-empty")

    if state.run_kind in ENGAGEMENT_SCOPED_KINDS and state.engagement_id is None:
        raise ValueError(
            f"engagement_id: required when run_kind={state.run_kind!r} "
            f"(only 'sensing' may omit it)"
        )
