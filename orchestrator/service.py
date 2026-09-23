"""The service API the UI (app/) calls (CLAUDE.md build brief P3 §4). Every
function here takes an `AppContext` first and is a thin, synchronous read or a
fast write against Delta/local persistence -- nothing here runs a node
(CLAUDE.md §2.1: "the web tier never executes a node"). `start_audit_run`
inserts the `runs` row and returns immediately; the ThreadExecutor picks the
`queued` run up (or runs it right away if `ctx.executor` is set).

STUB COMMIT: signatures only, so the UI (app/) can import and code against
this module's names before the implementation lands. Every function raises
NotImplementedError. Filled in by the next commit on this file.

Public API (signatures kept stable for the UI to import against):

    AppContext = dataclass(settings, persistence, skills_dir, data_source_factory,
                            export_storage, clock, executor)
    build_app_context(env=os.environ) -> AppContext
    list_skills(ctx) -> list[dict]
    get_skill(ctx, skill_id) -> dict | None
    list_governed_tables(ctx) -> list[dict]
    suggest_bindings(ctx, skill_id) -> dict[str, str | None]
    start_audit_run(ctx, *, skill_id, bindings, audit_period, objective, run_owner,
                     mode='playbook', review_plan_first=False, engagement_id='ENG-DEFAULT') -> str
    get_run(ctx, run_id) -> dict
    list_runs(ctx, filters=None) -> list[dict]
    list_trace_events(ctx, run_id=None) -> list[dict]
    list_management_actions(ctx, filters=None) -> list[dict]
    confirm_plan(ctx, run_id, actor) -> RunState
    sign_off(ctx, run_id, actor) -> RunState
    resume_run(ctx, run_id, actor) -> RunState
    get_run_payload(ctx, run_id) -> dict
    get_run_frames(ctx, run_id) -> dict[str, pandas.DataFrame]
    get_export(ctx, run_id, kind) -> tuple[str, bytes]
    health(ctx) -> dict
    ready(ctx) -> dict
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from orchestrator.config import Settings

FRAME_COLUMNS: dict[str, tuple[str, ...]] = {}


@dataclass
class AppContext:
    settings: Settings
    persistence: Any
    skills_dir: Path
    data_source_factory: Callable[[dict[str, str]], Any]
    export_storage: Any
    clock: Callable[[], str]
    executor: Any = None
    backend: str = "uc"
    local_data_root: Path | None = None


def build_app_context(env: dict | None = None) -> AppContext:
    raise NotImplementedError


def list_skills(ctx: AppContext) -> list[dict]:
    raise NotImplementedError


def get_skill(ctx: AppContext, skill_id: str) -> dict | None:
    raise NotImplementedError


def list_governed_tables(ctx: AppContext) -> list[dict]:
    raise NotImplementedError


def suggest_bindings(ctx: AppContext, skill_id: str) -> dict[str, str | None]:
    raise NotImplementedError


def start_audit_run(
    ctx: AppContext,
    *,
    skill_id: str,
    bindings: dict[str, str],
    audit_period: tuple[str, str],
    objective: str,
    run_owner: str,
    mode: str = "playbook",
    review_plan_first: bool = False,
    engagement_id: str = "ENG-DEFAULT",
) -> str:
    raise NotImplementedError


def get_run(ctx: AppContext, run_id: str) -> dict:
    raise NotImplementedError


def list_runs(ctx: AppContext, filters: dict | None = None) -> list[dict]:
    raise NotImplementedError


def list_trace_events(ctx: AppContext, run_id: str | None = None) -> list[dict]:
    raise NotImplementedError


def list_management_actions(ctx: AppContext, filters: dict | None = None) -> list[dict]:
    raise NotImplementedError


def confirm_plan(ctx: AppContext, run_id: str, actor: str):
    raise NotImplementedError


def sign_off(ctx: AppContext, run_id: str, actor: str):
    raise NotImplementedError


def resume_run(ctx: AppContext, run_id: str, actor: str):
    raise NotImplementedError


def get_run_payload(ctx: AppContext, run_id: str) -> dict:
    raise NotImplementedError


def get_run_frames(ctx: AppContext, run_id: str):
    raise NotImplementedError


def get_export(ctx: AppContext, run_id: str, kind: str) -> tuple[str, bytes]:
    raise NotImplementedError


def health(ctx: AppContext) -> dict:
    raise NotImplementedError


def ready(ctx: AppContext) -> dict:
    raise NotImplementedError
