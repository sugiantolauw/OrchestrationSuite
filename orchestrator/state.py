from __future__ import annotations

import dataclasses
import json
import math
from dataclasses import asdict, dataclass, field, fields
from datetime import date
from typing import Any, Literal

from orchestrator.errors import NodeContractViolation, NotJsonSafe
from orchestrator.timeutil import is_canonical_date, is_canonical_ts

RunKind = Literal[
    "fieldwork", "sensing", "assessment", "planning",
    "design_assessment", "reporting", "evidence",
]
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

RUN_KINDS: tuple[str, ...] = (
    "fieldwork", "sensing", "assessment", "planning",
    "design_assessment", "reporting", "evidence",
)
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
# Every run_kind is engagement-scoped except `sensing`, which is
# corpus-scoped and continuous rather than tied to one engagement
# (LIFECYCLE_design.md §2.2; CLAUDE.md §4.8/§4.10 item 2).
ENGAGEMENT_SCOPED_KINDS: tuple[str, ...] = (
    "fieldwork", "assessment", "planning",
    "design_assessment", "reporting", "evidence",
)


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
    phase_epoch: int = 1  # the state_version at which the run entered `phase` (CLAUDE.md §9C, B2)

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
    # Refs/scalars a lifecycle run_kind's own nodes write, e.g. {"kind":
    # "sensing", "snapshot_id": ..., "candidate_risk_ids": [...], "coverage":
    # {...}, "stop_reason": ...} (LIFECYCLE_design.md §2.3). Large content
    # lives in the module's own tables, keyed by run_id, not here. Unused by
    # fieldwork.
    module_output: dict = field(default_factory=dict)
    signoff: dict | None = None  # approver, timestamp, self_approved, sod_enforced (orchestrator/signoff_policy.py)
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


# The node function's job is to produce these; the pipeline loop never trusts a node
# to leave everything else alone -- it enforces it (B1). Every RunState field belongs
# to exactly one of these two sets. test_state.py asserts the partition is complete
# and disjoint against fields(RunState), so this can never silently drift.
NODE_OWNED: frozenset[str] = frozenset(
    {
        "data_assets",
        "uploaded_files",
        "profile_result",
        "plan",
        "plan_rationale",
        "test_results",
        "flagged_table",
        "reconciliation",
        "exceptions",
        "findings",
        "management_actions",
        "exports",
        "module_output",
        "profile_narrative",
        "classification_reasoning",
        "finding_narratives",
        "priority_rationale",
        "remediation_drafts",
        "exec_summary",
        "chart_captions",
        "events",
        "errors",
    }
)

LIFECYCLE: frozenset[str] = frozenset(
    {
        "run_id",
        "run_kind",
        "engagement_id",
        "skill_id",
        "skill_version",
        "mode",
        "phase",
        "next_node_index",
        "audit_period",
        "objective",
        "business_unit",
        "materiality",
        "options",
        "run_owner",
        "fingerprint_id",
        "state_version",
        "phase_epoch",
        "created_at",
        "started_at",
        "completed_at",
        "last_state_change_at",
        "current_node_attempt_id",
        "plan_confirmed",
        "plan_edits",
        "signoff",
        "status_reason",
        "status",
    }
)

_APPEND_ONLY_NODE_FIELDS = ("events", "errors")


def _field_value(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj[name]
    return getattr(obj, name)


def apply_node_output(current: "RunState", node_result: Any) -> "RunState":
    """Merges a node's output into `current`: NODE_OWNED fields come from
    `node_result`, every LIFECYCLE field is preserved unchanged from `current`.
    `node_result` may be a full RunState (the normal path, a node function's return
    value) or a plain dict holding only the node-owned fields (the recovery path,
    read back from `node_attempts.result_state_json`) -- both are read the same way.

    `events` and `errors` are append-only node-owned lists: node_result's list must
    start with current's list (the node may only append), or this raises
    NodeContractViolation. The merged value is node_result's own list, since the
    prefix check already proves it equals current's list plus a new tail.
    """
    updates: dict[str, Any] = {}
    for name in NODE_OWNED:
        if name in _APPEND_ONLY_NODE_FIELDS:
            continue
        updates[name] = _field_value(node_result, name)
    for name in _APPEND_ONLY_NODE_FIELDS:
        cur_list = getattr(current, name)
        new_list = _field_value(node_result, name)
        if list(new_list[: len(cur_list)]) != list(cur_list):
            raise NodeContractViolation(
                f"{name}: node result does not start with the current state's {name} "
                f"(a node may only append, never rewrite or drop entries)"
            )
        updates[name] = new_list
    return dataclasses.replace(current, **updates)


def node_owned_snapshot(state: "RunState") -> dict:
    return {name: getattr(state, name) for name in NODE_OWNED}


def node_owned_json(state: "RunState") -> str:
    snapshot = node_owned_snapshot(state)
    for name, value in snapshot.items():
        _walk(value, name)
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"))


def _walk(value: Any, path: str) -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NotJsonSafe(f"{path}: non-finite float {value!r}")
        return
    if isinstance(value, tuple):
        # Tuples don't round-trip through JSON (they come back as lists), so the only
        # place one is allowed is RunState.audit_period, which assert_json_safe unwraps
        # to a list before walking it. Anywhere else a tuple means the node put one in
        # by accident.
        raise NotJsonSafe(f"{path}: tuple is not JSON-safe outside audit_period ({value!r})")
    if isinstance(value, list):
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
        value = getattr(state, f.name)
        if f.name == "audit_period":
            _walk(list(value), f.name)
        else:
            _walk(value, f.name)


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

    if state.phase_epoch < 1:
        raise ValueError(f"phase_epoch: must be >= 1, got {state.phase_epoch!r}")

    for name in ("created_at", "last_state_change_at"):
        value = getattr(state, name)
        if not is_canonical_ts(value):
            raise ValueError(f"{name}: not a canonical timestamp: {value!r}")
    for name in ("started_at", "completed_at"):
        value = getattr(state, name)
        if value is not None and not is_canonical_ts(value):
            raise ValueError(f"{name}: not a canonical timestamp: {value!r}")
    if not is_canonical_date(start_s) or not is_canonical_date(end_s):
        raise ValueError(f"audit_period: dates must be YYYY-MM-DD: {state.audit_period!r}")
