"""The service API the UI (app/) calls (CLAUDE.md build brief P3 §4). Every
function here takes an `AppContext` first and is a thin, synchronous read or a
fast write against Delta/local persistence -- nothing here runs a node
(CLAUDE.md §2.1: "the web tier never executes a node"). `start_audit_run`
inserts the `runs` row and returns immediately; the ThreadExecutor picks the
`queued` run up (or runs it right away if `ctx.executor` is set).

Public API (signatures kept stable for the UI to import against):

    AppContext = dataclass(settings, persistence, skills_dir, data_source_factory,
                            export_storage, clock, executor)
    build_app_context(env=os.environ) -> AppContext
    list_skills(ctx) -> list[dict]
    get_skill(ctx, skill_id) -> dict | None
    list_governed_tables(ctx) -> list[dict]
    list_data_asset_cards(ctx, query='', limit=None) -> list[dict]
    suggest_bindings(ctx, skill_id) -> dict[str, str | None]
    start_audit_run(ctx, *, skill_id, bindings, audit_period, objective, run_owner,
                     mode='playbook', review_plan_first=False, engagement_id='ENG-DEFAULT') -> str
    get_run(ctx, run_id) -> dict
    list_runs(ctx, filters=None) -> list[dict]
    cross_run_totals(runs) -> dict   (pure; independent review 2026-09-24 gap #6)
    list_trace_events(ctx, run_id=None) -> list[dict]
    list_management_actions(ctx, filters=None) -> list[dict]
    get_actions_page_data(ctx, filters=None) -> tuple[list[dict], list[dict]]  (actions, runs)
    update_management_action(ctx, action_id, *, owner, status, target_date, response,
                              actor) -> dict   (independent review 2026-09-24 gap #3)
    confirm_plan(ctx, run_id, actor) -> RunState
    sign_off(ctx, run_id, actor) -> RunState
    resume_run(ctx, run_id, actor) -> RunState
    decide_candidate(ctx, run_id, candidate_id, *, decision, reason=None,
                      decided_severity=None, actor) -> dict   (P6 WP N9)
    edit_narrative(ctx, run_id, narrative_id, new_text, *, actor) -> dict   (P6 WP N9)
    regenerate_narration(ctx, run_id, actor) -> RunState   (P6 WP N9)
    get_run_payload(ctx, run_id) -> dict
    get_run_frames(ctx, run_id) -> dict[str, pandas.DataFrame]
    get_export(ctx, run_id, kind) -> tuple[str, bytes]
    get_upload_base_path(ctx) -> str
    upload_file(ctx, *, filename, content, uploaded_by, engagement_id='ENG-DEFAULT') -> dict
    list_uploaded_files(ctx, engagement_id=None) -> list[dict]
    propose_plan(ctx, *, skill_id, mode='playbook') -> dict
    health(ctx) -> dict
    ready(ctx) -> dict
    readiness_report(ctx, force=False) -> dict
    start_explorer_run(ctx, *, objective, sources, audit_period, run_owner,
                        engagement_id='ENG-DEFAULT', ..., supersedes_run_id=None) -> str
    get_explorer_review(ctx, run_id) -> dict
    edit_explorer_plan(ctx, run_id, edits, actor) -> RunState
    save_explorer_draft_skill(ctx, run_id, actor) -> dict
    publish_skill(ctx, skill_id, version, reviewer) -> dict
    resolve_run_skill(ctx, state) -> Skill | None

`ORCH_BACKEND=local` (env) selects LocalPersistence + a local-file
DataSourceAdapter for local/E2E runs (against synthetic_data/ by default) --
surfaced everywhere as data_mode "Local test data". Anything else selects
DeltaPersistence + UCTableDataSource (orchestrator/adapters/datasource_uc.py,
owned by the Unity Catalog work) against Unity Catalog, surfaced as
"Unity Catalog".
"""

from __future__ import annotations

import difflib
import functools
import hashlib
import json
import logging
import os
import re
import string
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import yaml

from orchestrator import runs as runs_module
from orchestrator.adapters.export_storage import LocalExportStorage, VolumeExportStorage
from orchestrator.adapters.persistence_delta import DeltaPersistence
from orchestrator.adapters.persistence_local import LocalPersistence
from orchestrator.config import Settings, load_settings
from orchestrator.contract import ContractViolation, LocalFileDataSource
from orchestrator.errors import (
    CandidateAlreadyDecided,
    CandidateNotFound,
    CandidateReasonRequired,
    CandidateSeverityRequired,
    CandidateSuperseded,
    ConfigError,
    ExplorerEditRejected,
    ExplorerInputError,
    ExplorerPlanNotConfirmable,
    FingerprintMismatch,
    InvalidRunId,
    NarrationDisabled,
    NarrationNodeUnavailable,
    NarrativeEditConflict,
    NarrativeEditNotAllowed,
    NarrativeEditRejected,
    NarrativeNotFound,
    NarrativeTargetNotFound,
    PlanIntegrityError,
    PromotionRequirementsNotMet,
    ReviewActionRefused,
    RunCodeRevisionStale,
    RunNotAwaitingSignoff,
    RunNotReady,
)
from orchestrator.executor import ThreadExecutor
from orchestrator.explorer.edit import apply_plan_edits, excluded_test_keys, parse_param_path
from orchestrator.explorer.materialise import check_materialised_skill, materialise
from orchestrator.explorer.validate import validate_proposal
from orchestrator.fingerprint import compute_fingerprint, hash_skill_content_entries, verify_fingerprint
from orchestrator.frames import not_testable_flags, read_frame_parquet
from orchestrator.identity import LazyRoleResolver
from orchestrator.nodes.context import NodeContext
from orchestrator.nodes.registry import NODES_FOR
from orchestrator.pipeline import NODE_STAGE_LABELS
from orchestrator.signoff_policy import evaluate_signoff, label_for
from orchestrator.skill_registry import register_skill
from orchestrator.skills import Skill, load_skill, load_skill_from_ledger, plan_test_flags
from orchestrator.source_bindings import (
    bindings_for_skill,
    load_source_bindings,
    suggested_values,
    volume_file_paths,
)
from orchestrator.state import RunState, to_json
from orchestrator.status import transition
from orchestrator.timeutil import utc_now

REPO_ROOT = Path(__file__).resolve().parent.parent

_LOG = logging.getLogger(__name__)

# get_run_frames' column contract, per contract source (CLAUDE.md build brief
# P3 §4): every declared contract column (already type-coerced by
# orchestrator.contract.validate_contract when the run executed), plus one 0/1
# int column per RF_* flag this run produced against that source (pandas
# nullable Int64 <NA> for a not_testable test's declared flag -- distinct from
# a tested-and-clean 0, CLAUDE.md NN14), plus the always-present join key
# columns __source and __row_key (back to flagged_rows/findings). Populated
# lazily per Skill the first time get_run_frames sees it.
FRAME_COLUMNS: dict[str, tuple[str, ...]] = {}


@dataclass
class AppContext:
    settings: Settings
    persistence: Any
    skills_dir: Path
    # Second arg (contract_sources: the Skill's contract.yaml `sources`
    # mapping) is optional -- callers with no Skill in scope (e.g.
    # list_governed_tables's empty-bindings call) omit it. A UC-backend
    # caller that DOES have a Skill passes it so a source bound to an
    # uploaded file's Volume path can be parsed correctly (CLAUDE.md §5 UI
    # item 5, orchestrator.adapters.datasource_volume_upload).
    data_source_factory: Callable[[dict[str, str], dict[str, dict] | None, str | None], Any]
    export_storage: Any
    clock: Callable[[], str]
    executor: Any = None
    # Not in the UI-facing signature list above, but needed to disambiguate
    # local-vs-UC behaviour (data_mode label, list_governed_tables/
    # suggest_bindings) without the UI ever branching on it itself.
    backend: str = "uc"
    local_data_root: Path | None = None
    # MLflow per-node tracing (CLAUDE.md §2.3, P2/P3 gate review item 4).
    # Defaults to NullTracing (a documented no-op) so every existing caller
    # that never passed one keeps behaving exactly as before this feature.
    tracing: Any = None
    # Independent review 2026-09-24 items 3/5: the ModelClient a node's LLM
    # calls, and /ready's model-endpoint checks, use. None on the local
    # backend (never a real WorkspaceClient in local/test runs); a real
    # DatabricksModelClient on the uc backend, built lazily.
    model_client: Any = None
    # Cached readiness prober (item 5) -- None until build_app_context wires
    # it; service.ready() falls back to the un-cached, warehouse-only check
    # when this is unset (keeps every existing direct AppContext(...)
    # construction, e.g. in tests, working unchanged).
    readiness: Any = None
    # P3/P4 perf gap review 2026-09-25, warm-pool follow-up: a single
    # orchestrator.adapters.datasource_uc._UCConnectionPool, built once in
    # build_app_context and OUTLIVING any one UCTableDataSource instance --
    # `_uc_factory` and `_build_explorer_data_source` both hand it to every
    # UCTableDataSource they build (via `pool=`), so start_audit_run's own
    # version resolution and every later node's reads (build_node_context)
    # share the SAME warm connections instead of each paying a cold open.
    # None on the local backend, and for every existing direct AppContext(...)
    # construction (tests) that never sets it -- UCTableDataSource treats a
    # missing pool exactly as before this change: it builds and owns a
    # private one.
    uc_pool: Any = None


# ── construction ──────────────────────────────────────────────────────────────


def _all_skill_source_configs(skills_dir: Path) -> dict[str, dict]:
    combined: dict[str, dict] = {}
    for d in sorted(Path(skills_dir).iterdir()):
        if not d.is_dir():
            continue
        contract_path = d / "contract.yaml"
        if contract_path.is_file():
            data = yaml.safe_load(contract_path.read_text()) or {}
            for name, cfg in (data.get("sources") or {}).items():
                combined.setdefault(name, cfg)
    return combined


def _local_data_source_factory(skills_dir: Path, data_root: Path) -> Callable[[dict[str, str]], Any]:
    configs = _all_skill_source_configs(skills_dir)

    def factory(bindings: dict[str, str], contract_sources: dict[str, dict] | None = None, skill_id: str | None = None):
        sources: dict[str, dict] = {}
        for name in bindings:
            cfg = dict(configs.get(name) or {})
            file_override = bindings.get(name)
            if file_override:
                cfg["file"] = file_override
            sources[name] = cfg
        return LocalFileDataSource(root_dir=data_root, sources=sources)

    return factory


@functools.lru_cache(maxsize=8)
def _load_source_bindings_cached(path: str) -> dict:
    return load_source_bindings(path)


def configured_bindings_for_skill(ctx: AppContext, skill_id: str | None) -> dict[str, dict]:
    """Independent review 2026-09-24 item 1: this Skill's SOURCE_BINDINGS
    entries (orchestrator.source_bindings), `{}` when SOURCE_BINDINGS is
    unset or the Skill has none configured. Cached per bindings-file path
    for the process lifetime -- a config-file edit takes effect on restart,
    same as every other config value this App reads at startup."""
    if not skill_id or not ctx.settings.source_bindings_path:
        return {}
    config = _load_source_bindings_cached(ctx.settings.source_bindings_path)
    return bindings_for_skill(config, skill_id)


def _skill_dir_for(ctx: AppContext, skill_id: str | None) -> Path:
    if not skill_id:
        raise ValueError("skill_id is required")
    for d in sorted(ctx.skills_dir.iterdir()):
        manifest_path = d / "manifest.yaml"
        if manifest_path.is_file():
            manifest = yaml.safe_load(manifest_path.read_text()) or {}
            if manifest.get("id") == skill_id:
                return d
    raise ValueError(f"no skill directory under {ctx.skills_dir} with manifest id={skill_id!r}")


def load_skill_by_id(ctx: AppContext, skill_id: str, version: str | None = None) -> Skill:
    """docs/specs/P6_P8_explorer_llm_design.md §4.13 point 5: a repo Skill
    directory first (the existing _skill_dir_for lookup, unchanged) -- if
    none exists under this skill_id, the skill_versions ledger row this
    skill_id/version was saved as (an Explorer-saved draft, origin
    'explorer_saved', §4.13 point 2's fixed version "0.1.0"). Running a
    saved draft Skill is then an ordinary Playbook run: start_audit_run and
    the Playbook branch of resolve_run_skill both go through this."""
    try:
        skill_dir = _skill_dir_for(ctx, skill_id)
        return load_skill(skill_dir)
    except ValueError:
        pass
    resolved_version = version or "0.1.0"
    row = ctx.persistence.get_skill_version(skill_id, resolved_version)
    if row is None:
        raise ValueError(
            f"no skill directory under {ctx.skills_dir} and no skill_versions row "
            f"({skill_id!r}, {resolved_version!r}) -- unknown skill_id"
        )
    return load_skill_from_ledger(row)


def resolve_run_skill(ctx: AppContext, state: RunState) -> Skill | None:
    """docs/specs/P6_P8_explorer_llm_design.md §4.11: Playbook loads from the
    repo directory (or the ledger, via load_skill_by_id, for a Playbook run
    of a saved Explorer draft) exactly as before. An Explorer run has no
    Skill at all before confirm_plan -- the plan-phase nodes' Explorer
    branches (discover/profile/plan in orchestrator.nodes.fieldwork) never
    read ctx.skill, so None is the correct answer, not a fabricated one.
    After confirmation it loads the EXPLORER-<run_id> ledger row confirm_plan
    materialised, and refuses to proceed (PlanIntegrityError -- the same
    handling as a fingerprint mismatch, CLAUDE.md §3 non-negotiable 8) if
    that row is missing or its content_hash no longer matches
    state.confirmed_plan_hash."""
    if state.mode == "explorer":
        if not state.confirmed_plan_hash:
            return None
        skill_id = f"EXPLORER-{state.run_id}"
        row = ctx.persistence.get_skill_version(skill_id, "run")
        if row is None:
            raise PlanIntegrityError(
                f"run {state.run_id!r}: plan_confirmed=True (hash {state.confirmed_plan_hash[:12]}) "
                f"but no {skill_id!r} skill_versions row exists"
            )
        skill = load_skill_from_ledger(row)
        if skill.content_hash != state.confirmed_plan_hash:
            raise PlanIntegrityError(
                f"run {state.run_id!r}: confirmed_plan_hash {state.confirmed_plan_hash!r} does not "
                f"match the ledger Skill's content_hash {skill.content_hash!r} -- the materialised "
                f"Skill may have been tampered with"
            )
        return skill
    return load_skill_by_id(ctx, state.skill_id, state.skill_version)


_DATA_FILE_EXTS = (".csv", ".xlsx", ".xls", ".parquet")
_FORMAT_BY_EXT = {".csv": "csv", ".xlsx": "xlsx", ".xls": "xlsx", ".parquet": "parquet"}
_SOURCE_NAME_INVALID_RE = re.compile(r"[^a-z0-9]+")


def _explorer_source_name(ref: str, taken: set[str]) -> str:
    """docs/specs/P6_P8_explorer_llm_design.md §4.2: "Take the last
    identifier segment (the table name or the file stem), lowercase it,
    replace runs of [^a-z0-9] with _, strip, prefix s_ if it starts with a
    digit, and suffix _2, _3 on collision, in input order.\""""
    if "/" in ref or "\\" in ref:
        stem = Path(ref).stem
    elif ref.lower().endswith(_DATA_FILE_EXTS):
        stem = Path(ref).stem
    elif "." in ref:
        stem = ref.rsplit(".", 1)[-1]
    else:
        stem = ref
    name = _SOURCE_NAME_INVALID_RE.sub("_", stem.lower()).strip("_") or "source"
    if name[0].isdigit():
        name = f"s_{name}"
    base, n = name, 2
    while name in taken:
        name = f"{base}_{n}"
        n += 1
    taken.add(name)
    return name


def _infer_source_format(ref: str) -> str:
    ext = Path(ref).suffix.lower()
    fmt = _FORMAT_BY_EXT.get(ext)
    if fmt is None:
        raise ExplorerInputError(
            f"start_explorer_run: cannot infer a file format for {ref!r} -- "
            f"expected one of {sorted(_FORMAT_BY_EXT)}"
        )
    return fmt


def _default_reference_skill_ids(ctx: AppContext) -> list[str]:
    """CLAUDE.md §4.5 / docs/specs/P6_P8_explorer_llm_design.md §4.4: the 1-2
    reference Skills the Explorer planner sees as worked examples of how a
    test, its finding rules, its metric names and its threshold references
    are actually written. Independent review round 2 (BUG-EXPLORER-2): every
    real planner/repair call in this workspace passed none (`start_explorer_
    run`'s `reference_skill_ids` parameter was never resolved from anywhere
    when a caller left it unset), and the model's own metric names,
    trigger/severity expressions and template placeholders -- none of which
    have a schema-level format signal, only a worked example teaches them --
    were consistently wrong in the same ways a worked example would have
    shown correctly. `Settings.explorer_reference_skill_ids` is an explicit
    override when set (config.py's own docstring for that field); otherwise
    the first two valid (successfully `load_skill()`-able) repo Skill ids,
    sorted -- an invalid repo Skill directory is simply not offered, never a
    reason to fail run creation."""
    override = getattr(ctx.settings, "explorer_reference_skill_ids", None)
    if override:
        return list(override)
    ids: list[str] = []
    for d in sorted(Path(ctx.skills_dir).iterdir()):
        if not d.is_dir():
            continue
        try:
            ids.append(load_skill(d).skill_id)
        except Exception:  # noqa: BLE001 -- an invalid repo Skill directory is simply not offered
            continue
    return sorted(ids)[:2]


def _explorer_inputs_hash(ctx: AppContext, reference_skill_ids: list[str]) -> str:
    """docs/specs/P6_P8_explorer_llm_design.md §4.2: the Explorer run
    fingerprint's skill_content_hash override -- there is no Skill on disk
    yet (skill_dir=None), so this hashes exactly what the plan node's own
    proposal depends on: the wire schema, the validator's rule version, and
    (BUG-EXPLORER-2 fix) each reference Skill's OWN content_hash, keyed by
    id -- `{"reference_skills": {id: content_hash}}`, the shape this design
    doc's §4.2 documents. Recomputed identically by build_run_fingerprint on
    every executor pass and at resume (CLAUDE.md §4.1), so it never drifts
    from what start_explorer_run pinned unless a reference Skill's own repo
    directory genuinely changed underneath it -- the same kind of drift a
    Playbook run's fingerprint already surfaces for its own Skill."""
    from orchestrator.explorer.validate import EXPLORER_VALIDATOR_VERSION
    from orchestrator.explorer.wire_schema import WIRE_SCHEMA_SHA256

    reference_skills = {
        skill_id: load_skill_by_id(ctx, skill_id).content_hash for skill_id in sorted(reference_skill_ids)
    }
    payload = {
        "reference_skills": reference_skills, "wire_schema_sha256": WIRE_SCHEMA_SHA256,
        "validator_rules_version": EXPLORER_VALIDATOR_VERSION,
    }
    return "explorer-inputs:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _build_explorer_data_source(ctx: AppContext, state: RunState):
    """The DataSourceAdapter for an Explorer run's OWN bound sources
    (state.data_assets / options.explorer.sources) -- built directly per
    backend rather than through ctx.data_source_factory, because that
    factory's `contract_sources` argument is keyed by a repo Skill's own
    contract.yaml source configs (orchestrator.service._local_data_source_
    factory), which an Explorer source (chosen at run setup, not declared by
    any Skill) never appears in. Used by every Explorer-mode node (via
    build_node_context) and by the plan-review/confirm service functions
    below (V-C3's distinct_count check, and re-validation after edits)."""
    options = (state.options or {}).get("explorer", {})
    sources_cfg = {s["name"]: s for s in options.get("sources", [])}
    if ctx.backend == "local":
        # An `upload`-kind entry's `b["table_fqn"]` is the upload's OWN
        # recorded volume_path (_resolve_explorer_sources /
        # _resolve_explorer_upload_entries pin it that way for every
        # backend, never the upload_id) -- an absolute path under
        # ctx.export_storage's root, so LocalFileDataSource reads it
        # correctly regardless of `root_dir` (Path(root)/absolute == the
        # absolute path). Its sha256 is re-verified on every read by
        # LocalFileDataSource.read_population against this binding's
        # pinned `version` (state.data_assets), the same re-verification
        # VolumeUploadAwareDataSource performs for a non-local upload below.
        local_sources: dict[str, dict] = {}
        for b in state.data_assets:
            name = b["source"]
            cfg = sources_cfg.get(name, {})
            entry: dict = {"format": cfg.get("format") or "csv", "file": cfg.get("file") or b["table_fqn"]}
            if cfg.get("sheet"):
                entry["sheet"] = cfg["sheet"]
            local_sources[name] = entry
        return LocalFileDataSource(root_dir=ctx.local_data_root, sources=local_sources)

    from orchestrator.adapters.datasource_uc import UCTableDataSource
    from orchestrator.adapters.datasource_volume_upload import VolumeUploadAwareDataSource

    bindings = {b["source"]: b["table_fqn"] for b in state.data_assets}
    # P3/P4 perf gap review 2026-09-25, warm-pool follow-up: shares
    # AppContext.uc_pool the same way build_app_context's _uc_factory does
    # -- an Explorer run's plan/execute-phase reads reuse the SAME warm
    # connections a Playbook run's would, rather than each Explorer pass
    # paying its own cold open. `ctx.uc_pool` is None on the local backend
    # and for any bare AppContext(...) test construction, in which case
    # UCTableDataSource falls back to a private pool, exactly as before.
    table_source = UCTableDataSource(ctx.settings, bindings, pool=ctx.uc_pool)
    # An `upload`-kind Explorer source's table_fqn is the uploaded file's own
    # volume_path (start_explorer_run pins it that way), so the SAME
    # VolumeUploadAwareDataSource wrapper Playbook uses (build_app_context's
    # _uc_factory) reads it from the Volume via the Files API instead of
    # sending it through the UC table SQL path -- there is no Explorer-
    # specific `contract_sources`, so format is inferred from the file's own
    # extension (that wrapper's own fallback, never guessed here).
    return VolumeUploadAwareDataSource(
        table_source=table_source, bindings=bindings, contract_sources={},
        persistence=ctx.persistence, export_storage=ctx.export_storage,
    )


def _close_data_source(data_source: Any) -> None:
    """BUG-UCPOOL-1 (independent review round 3, 2026-09-25): belt-and-braces
    close for every data source `ctx.data_source_factory`/`UCTableDataSource(
    ...)` builds outside a `NodeContext` (executor.py._run_one closes the
    one attached to a run's own NodeContext). The pool fix in
    orchestrator.adapters.datasource_uc means a connection is never held
    across calls any more, so this is no longer load-bearing for leak
    prevention -- but a private (no `pool=` given) UCTableDataSource still
    OWNS the connections it opens, and closing it here is what actually
    releases them rather than leaving them for garbage collection. `close`
    is optional -- LocalFileDataSource has none -- and a failure here must
    never surface as a failure of whatever real work this data source was
    built for."""
    close = getattr(data_source, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:
        _LOG.exception("data_source.close() failed")


def _explorer_run_sources_for_materialise(options: dict) -> list[dict]:
    """`orchestrator.explorer.materialise.materialise`'s `run_sources` shape
    (`{"name", "kind", "format", "file"}`) from `options.explorer.sources`
    (§4.2), resolving `file` the SAME way `_build_explorer_data_source`
    resolves a local source's own file binding: an auditor-given `file`
    override when there is one, otherwise the source's own `ref` for a
    `local_file`/`upload` source (a `uc_table` source has no file at all).
    Without this fallback, `contract.yaml`'s schema requires `file` for
    every csv/xlsx/parquet format entry (§4.11's contract schema change)
    and materialisation would fail outright for the common case of an
    Explorer source bound directly by its own path/upload id, never given a
    SEPARATE file override."""
    out = []
    for s in options.get("sources", []):
        file_ = s.get("file")
        if not file_ and s.get("kind") in ("local_file", "upload"):
            file_ = s.get("ref")
        out.append({"name": s["name"], "kind": s["kind"], "format": s.get("format"), "file": file_})
    return out


def _build_explorer_llm(ctx: AppContext):
    """docs/specs/P6_P8_explorer_llm_design.md §4.12 item 2: one LLMGateway/
    FilePromptRepository pair per executor pass, shared by every node in it.
    On the local backend, `client` stays exactly what ctx.model_client is
    (None unless a test overrode it) -- never a real DatabricksModelClient,
    so a local/test run can never make a live network call by accident; the
    gateway itself still degrades cleanly to 'unavailable' whenever the
    resolved endpoint (Settings.model_sonnet/model_gpt_oss) is unset,
    exactly the CLAUDE.md §6 NN13 behaviour a real deployment relies on."""
    from orchestrator.config import NODE_MODELS
    from orchestrator.llm.gateway import LLMGateway
    from orchestrator.llm.prompts import FilePromptRepository

    client = ctx.model_client
    if client is None and ctx.backend != "local":  # pragma: no cover -- real client, RUN_LIVE_LLM=1 only
        from orchestrator.adapters.model_databricks import DatabricksModelClient

        client = DatabricksModelClient()
    llm = LLMGateway(
        settings=ctx.settings, client=client, persistence=ctx.persistence, node_models=NODE_MODELS,
        clock=ctx.clock, retry_backoff_s=getattr(ctx.settings, "llm_retry_backoff_s", 5.0),
        timeout_s=getattr(ctx.settings, "llm_timeout_s", 180.0),
        max_transport_attempts=getattr(ctx.settings, "llm_max_transport_attempts", 5),
        retry_backoff_max_s=getattr(ctx.settings, "llm_retry_backoff_max_s", 75.0),
    )
    return llm, FilePromptRepository()


def _edit_before_value(proposal: dict, prior_edits: list[dict], edit: dict):
    """The value `edit` is about to change, as of right before it -- i.e.
    the base proposal narrowed by every edit already recorded (`prior_edits`,
    in order), never the edit's OWN op alone. For `exclude_test`/
    `include_test` this is "was the test included right before this edit"
    (`excluded_test_keys(prior_edits)`, so a second toggle of the same test
    records a correct before/after pair rather than a constant guessed from
    the op name). For `set_column`/`set_enum`/`set_threshold` it is the
    param/threshold value in `apply_plan_edits(proposal, prior_edits)` --
    the latest value any earlier edit in this run's history left it at."""
    op = edit["op"]
    if op in ("exclude_test", "include_test"):
        return edit["test_key"] not in excluded_test_keys(prior_edits)
    effective_before = apply_plan_edits(proposal, prior_edits)
    if op in ("set_column", "set_enum"):
        test = next((t for t in effective_before.get("tests", []) if t["key"] == edit.get("test_key")), None)
        if test is None:
            return None
        try:
            node = test
            path = parse_param_path(edit["param_path"])
            for part in path[:-1]:
                node = node[part]
            return node[path[-1]]
        except (KeyError, IndexError, TypeError):
            return None
    if op == "set_threshold":
        th = next((t for t in effective_before.get("thresholds", []) if t["id"] == edit.get("threshold_id")), None)
        return th.get("value") if th else None
    return None


def _describe_edit(edit: dict) -> str:
    op = edit["op"]
    if op == "exclude_test":
        return f"Excluded test {edit['test_key']}"
    if op == "include_test":
        return f"Included test {edit['test_key']}"
    if op in ("set_column", "set_enum"):
        return f"Set {edit['test_key']}.{edit['param_path']}: {edit.get('value')!r}"
    if op == "set_threshold":
        return f"Set threshold {edit['threshold_id']}: {edit.get('value')!r}"
    return f"{op} edit"


def _compute_run_fingerprint(
    ctx: AppContext, skill_dir: Path, source_table_versions: dict[str, str],
    uploaded_file_hashes: dict[str, str] | None = None,
) -> dict:
    reference_dir = skill_dir / "reference"
    reference_files = (
        sorted(p for p in reference_dir.rglob("*") if p.is_file()) if reference_dir.is_dir() else []
    )
    return compute_fingerprint(
        settings=ctx.settings,
        source_table_versions=source_table_versions,
        uploaded_file_hashes=uploaded_file_hashes or {},
        skill_dir=skill_dir,
        requirements_path=REPO_ROOT / "requirements.txt",
        prompts_dirs=[skill_dir / "prompts"],
        reference_files=reference_files,
        code_revision=None,
    )


def build_node_context(ctx: AppContext, state: RunState) -> NodeContext:
    # resolve_run_skill (docs/specs/P6_P8_explorer_llm_design.md §4.11)
    # returns None for an Explorer run before confirm_plan, and the
    # EXPLORER-<run_id> ledger Skill after it -- EVERY Explorer pass (not
    # only before confirmation) builds its data source from the run's OWN
    # bindings (state.data_assets/options.explorer.sources), never through
    # ctx.data_source_factory: that factory's `contract_sources` argument is
    # resolved from `skills_dir`'s REPO Skills at AppContext-construction
    # time (_all_skill_source_configs), so it has no way to see a
    # confirmed-but-never-published Explorer ledger Skill's own
    # contract.yaml -- state.skill_id is also None throughout an Explorer
    # run, which the Playbook factory needs to look anything up by. A
    # Playbook run (state.mode == "playbook") is unaffected: it always goes
    # through ctx.data_source_factory, exactly as before.
    skill = resolve_run_skill(ctx, state)
    if state.mode == "explorer":
        data_source = _build_explorer_data_source(ctx, state)
    else:
        bindings = {b["source"]: b["table_fqn"] for b in state.data_assets}
        data_source = ctx.data_source_factory(bindings, skill.contract.get("sources", {}), state.skill_id)

    # §4.12 item 2: an Explorer run's plan node needs a live LLMGateway/
    # FilePromptRepository pair; Playbook's execute-phase nodes (unchanged
    # by this step) ignore both regardless of whether they are set.
    llm = prompts = None
    explorer_reference_skills: list[Skill] = []
    if state.mode == "explorer":
        llm, prompts = _build_explorer_llm(ctx)
        # BUG-EXPLORER-2: load this RUN's own pinned reference_skill_ids
        # (resolved once at start_explorer_run, §4.5) as real Skill objects
        # for the plan node's prompt -- never re-resolved from Settings
        # here, so a later config change never changes what an in-flight
        # run's own planner call sees.
        ref_ids = ((state.options or {}).get("explorer") or {}).get("reference_skill_ids") or []
        explorer_reference_skills = [load_skill_by_id(ctx, skill_id) for skill_id in ref_ids]

    return NodeContext(
        settings=ctx.settings,
        persistence=ctx.persistence,
        data_source=data_source,
        skill=skill,
        clock=ctx.clock,
        export_storage=ctx.export_storage,
        backend=ctx.backend,
        model_client=ctx.model_client,
        llm=llm,
        prompts=prompts,
        explorer_reference_skills=explorer_reference_skills,
    )


# P3/P4 perf gap review 2026-09-25: register_skill's own work (skill_registry.
# register_skill) is a guaranteed no-op once this EXACT (skill_id, version,
# content_hash) has already been recorded -- record_skill_version no-ops on a
# repeat with the same hash, and upsert_risks/upsert_controls re-write the
# SAME rows from the SAME risk_control.yaml every single call (measured live
# against the real warehouse: ~80s total for SKILL-001's register_skill on a
# call that changed nothing, dominated by upsert_risks/upsert_controls doing
# one SELECT + one UPDATE/INSERT per risk/control, every run). Cached
# in-process only (never persisted, same pattern as datasource_uc.py's
# _ROW_COUNT_CACHE/_TABLE_LISTING_CACHE) -- a fresh worker, or a genuine Skill
# content change (a different content_hash, which this checks exactly, never
# fuzzily), always re-registers for real; this can never mask a real edit or
# skip seeding a risk/control that has never actually been written. Keyed on
# id(ctx.persistence) as well as the Skill identity -- a real deployment has
# exactly one persistence instance for the process's lifetime, so this never
# matters there, but the test suite builds a fresh persistence per test
# (often over the SAME on-disk Skill, so the same content_hash) within one
# pytest process; without this, a second test's freshly-created, empty
# persistence would wrongly inherit "already registered" from a PRIOR test's
# persistence instance and never get its own skill_versions/risks/controls
# rows written at all.
_REGISTERED_SKILL_VERSIONS: set[tuple[int, str, str, str]] = set()


def _ensure_skill_registered(ctx: AppContext, skill, actor: str, now: str) -> None:
    key = (id(ctx.persistence), skill.skill_id, skill.version, skill.content_hash)
    if key in _REGISTERED_SKILL_VERSIONS:
        return
    register_skill(skill, ctx.persistence, actor=actor, now=now)
    _REGISTERED_SKILL_VERSIONS.add(key)


def _flat_file_hashes(ctx: AppContext, skill_id: str | None, path_versions: dict[str, str]) -> dict[str, str]:
    """Restricts `{bound value: version}` (e.g. `data_assets`' table_fqn ->
    version, or `bindings[name] -> resolved version`) to the entries whose
    bound value is a flat file's Volume path -- an ad-hoc upload, or an
    independent review 2026-09-24 item 1 SOURCE_BINDINGS `volume_file` entry
    for this Skill -- the set `run_fingerprints.uploaded_file_hashes` pins
    (CLAUDE.md §4.1 P1A). Shared by start_audit_run and build_run_fingerprint
    so a resumed/verified run's recomputed fingerprint always matches what
    was captured at creation."""
    uploaded_volume_paths = {r["volume_path"] for r in ctx.persistence.list_uploaded_files()}
    configured_paths = set(volume_file_paths(configured_bindings_for_skill(ctx, skill_id)))
    flat_paths = uploaded_volume_paths | configured_paths
    return {path: version for path, version in path_versions.items() if path in flat_paths}


def _build_explorer_run_fingerprint(ctx: AppContext, state: RunState) -> dict:
    """docs/specs/P6_P8_explorer_llm_design.md §4.2/§7: recomputed IDENTICALLY
    to what start_explorer_run pinned -- skill_dir=None (there is no Skill on
    disk), skill_content_hash overridden to the same explorer_inputs_hash
    (a pure function of the run's OWN options, never of anything a Skill
    directory could hold), never resolve_run_skill (that would return None
    before confirmation and a DIFFERENT, per-run ledger Skill after it --
    the fingerprint is immutable at creation and never depends on which
    Skill a run's plan was eventually confirmed into, §7's own table)."""
    options = (state.options or {}).get("explorer", {})
    pinned_versions = {b["source"]: b["version"] for b in state.data_assets}
    uploaded_file_hashes = _flat_file_hashes(
        ctx, None, {b["table_fqn"]: b["version"] for b in state.data_assets}
    )
    return compute_fingerprint(
        settings=ctx.settings, source_table_versions=pinned_versions,
        uploaded_file_hashes=uploaded_file_hashes, skill_dir=None,
        requirements_path=REPO_ROOT / "requirements.txt",
        prompts_dirs=[REPO_ROOT / "orchestrator" / "prompts" / "explorer"],
        reference_files=[], code_revision=None,
        skill_content_hash=_explorer_inputs_hash(ctx, options.get("reference_skill_ids", [])),
    )


def build_run_fingerprint(ctx: AppContext, state: RunState) -> dict:
    if state.mode == "explorer":
        return _build_explorer_run_fingerprint(ctx, state)
    skill = load_skill_by_id(ctx, state.skill_id, state.skill_version)
    skill_dir = skill.skill_dir
    pinned_versions = {b["source"]: b["version"] for b in state.data_assets}
    # Same reconstruction as start_audit_run: a binding whose value is a
    # recorded upload's volume_path -- or a configured Volume file's path --
    # is pinned as an uploaded_file_hash too, so a resumed/verified run's
    # recomputed fingerprint matches the one captured at creation
    # (CLAUDE.md §4.1 "verified at every executor pass").
    uploaded_file_hashes = _flat_file_hashes(
        ctx, state.skill_id, {b["table_fqn"]: b["version"] for b in state.data_assets}
    )
    return _compute_run_fingerprint(ctx, skill_dir, pinned_versions, uploaded_file_hashes)


def build_app_context(env: dict | None = None) -> AppContext:
    env = os.environ if env is None else env
    settings = load_settings(env)
    backend = (env.get("ORCH_BACKEND") or "").strip().lower()
    skills_dir = Path(env.get("SKILLS_DIR") or (REPO_ROOT / "skills"))
    clock = utc_now

    # CLAUDE.md §2.3 / P2/P3 gate review item 4: constructed once, shared by
    # every run this App executes. Never raises -- MLflowTracingAdapter's own
    # constructor catches everything and reports `available=False` instead
    # (orchestrator.pipeline.run_phase logs the ONE "tracing unavailable"
    # trace_event per run that follows from that; App startup is never
    # blocked by a tracing backend problem).
    from orchestrator.adapters.tracing_mlflow import MLflowTracingAdapter

    tracing_kwargs = {}
    if settings.mlflow_experiment_path:
        tracing_kwargs["experiment_name"] = settings.mlflow_experiment_path
    tracing = MLflowTracingAdapter(tracking_uri=settings.mlflow_tracking_uri, **tracing_kwargs)

    if backend == "local":
        db_path = env.get("ORCH_LOCAL_DB") or str(REPO_ROOT / ".local" / "orchestrator.db")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        persistence = LocalPersistence(db_path)
        persistence.migrate()
        data_root = Path(env.get("ORCH_LOCAL_DATA_ROOT") or (REPO_ROOT / "synthetic_data"))
        data_source_factory = _local_data_source_factory(skills_dir, data_root)
        export_root = Path(env.get("ORCH_LOCAL_EXPORT_ROOT") or (REPO_ROOT / ".local" / "exports"))
        export_storage = LocalExportStorage(root_dir=export_root)
        ctx = AppContext(
            settings=settings, persistence=persistence, skills_dir=skills_dir,
            data_source_factory=data_source_factory, export_storage=export_storage,
            clock=clock, backend="local", local_data_root=data_root, tracing=tracing,
        )
    else:
        persistence = DeltaPersistence(settings)
        persistence.migrate()

        # P3/P4 perf gap review 2026-09-25, warm-pool follow-up: built ONCE
        # here and shared via AppContext.uc_pool (see that field's own
        # comment) -- _uc_factory and _build_explorer_data_source both hand
        # it to every UCTableDataSource they build, so a cold connection
        # open happens at most once per process. Lazily filled: constructing
        # the pool object does no I/O, and checkout() opens nothing until
        # the first real caller needs a connection (CLAUDE.md §11 idle-cost
        # incident -- "zero connections while idle" still holds). The
        # try/except mirrors _uc_factory's own guarded import just below,
        # for the same reason: this module may not exist yet in this
        # checkout.
        uc_pool = None
        try:
            from orchestrator.adapters.datasource_uc import _default_connection_factory, _UCConnectionPool

            uc_pool = _UCConnectionPool(
                lambda: _default_connection_factory(settings),
                max_size=settings.max_connections,
                checkout_timeout_s=30.0,
            )
        except ImportError:  # pragma: no cover - depends on a sibling agent's file
            pass

        def _uc_factory(bindings: dict[str, str], contract_sources: dict[str, dict] | None = None,
                        skill_id: str | None = None, _settings=settings, _persistence=persistence):
            try:
                from orchestrator.adapters.datasource_uc import UCTableDataSource
            except ImportError as exc:  # pragma: no cover - depends on a sibling agent's file
                from orchestrator.errors import ConfigError

                raise ConfigError(
                    "orchestrator.adapters.datasource_uc.UCTableDataSource is not available "
                    "yet -- the Unity Catalog source-data work has not landed in this checkout"
                ) from exc
            # `ctx` is defined further below in this same build_app_context()
            # call -- resolved at CALL time (late-bound closure), never at
            # definition time, and _uc_factory is only ever called after ctx
            # is fully constructed (same pattern this closure already relies
            # on for configured_bindings_for_skill(ctx, skill_id) below).
            table_source = UCTableDataSource(_settings, bindings, pool=ctx.uc_pool)
            # A source bound to an uploaded file's Volume path, or to a
            # SOURCE_BINDINGS-configured Volume file (independent review
            # 2026-09-24 item 1), is read from the Volume via the Files API,
            # not sent through the SQL path that a Volume path cannot
            # satisfy (CLAUDE.md §5 UI item 5, §11 corporate-workspace
            # assessment). Wrapping unconditionally costs nothing when no
            # binding is actually a flat file -- every call just falls
            # through to `table_source` unchanged.
            from orchestrator.adapters.datasource_volume_upload import VolumeUploadAwareDataSource

            configured = configured_bindings_for_skill(ctx, skill_id)
            return VolumeUploadAwareDataSource(
                table_source=table_source,
                bindings=bindings,
                contract_sources=contract_sources or {},
                persistence=_persistence,
                export_storage=export_storage,
                configured_volume_paths=volume_file_paths(configured),
            )

        export_storage = VolumeExportStorage(volume_root=settings.volume) if settings.volume else None

        # Independent review 2026-09-24 item 3/5: built once, lazily
        # (DatabricksModelClient's own __init__ does no network I/O -- the
        # WorkspaceClient is only constructed on the first real call), and
        # shared by every node's LLMGateway and by /ready's model-endpoint
        # checks.
        from orchestrator.adapters.model_databricks import DatabricksModelClient

        model_client = DatabricksModelClient()

        ctx = AppContext(
            settings=settings, persistence=persistence, skills_dir=skills_dir,
            data_source_factory=_uc_factory, export_storage=export_storage,
            clock=clock, backend="uc", tracing=tracing, model_client=model_client,
            uc_pool=uc_pool,
        )

    source_bindings_config = _load_source_bindings_cached(settings.source_bindings_path) if settings.source_bindings_path else {}
    from orchestrator.readiness import CachedReadiness

    ctx.readiness = CachedReadiness(
        persistence=ctx.persistence, export_storage=ctx.export_storage, settings=ctx.settings,
        source_bindings_config=source_bindings_config, model_client=ctx.model_client, clock=clock,
        ttl_s=getattr(settings, "readiness_cache_ttl_s", 120.0),
    )

    worker_id = env.get("ORCH_WORKER_ID") or f"worker-{uuid.uuid4().hex[:8]}"
    executor = ThreadExecutor(
        persistence=persistence,
        settings=settings,
        worker_id=worker_id,
        ctx_factory=lambda run_id: build_node_context(ctx, persistence.load_state(run_id)),
        fingerprint_factory=lambda run_id: build_run_fingerprint(ctx, persistence.load_state(run_id)),
        clock=clock,
        nodes_for=NODES_FOR,
        tracing=tracing,
        poll_interval_s=getattr(settings, "executor_active_poll_interval_s", 30.0),
        idle_poll_interval_s=getattr(settings, "executor_idle_poll_interval_s", 0.0),
    )
    ctx.executor = executor
    return ctx


# ── Skills ────────────────────────────────────────────────────────────────────


def _read_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _yaml_from_files(files: dict[str, str], name: str) -> dict:
    text = files.get(name)
    if not text:
        return {}
    return yaml.safe_load(text) or {}


def _ledger_skill_card(ctx: AppContext, row: dict) -> dict:
    """docs/specs/P6_P8_explorer_llm_design.md §4.13 point 4: the list_skills/
    get_skill item shape for a saved Explorer draft (origin='explorer_saved'
    skill_versions row, no repo directory under ctx.skills_dir) -- built from
    the ledger's own in-memory files (record_skill_version's `content`),
    never the filesystem. `catalogue_tests`/`contract_sources` are extra,
    additive fields (not on a repo skill's card) that
    src/platform/methodology.py's stub methodology page reads for its
    "Planned test catalogue"/"Planned data sources" sections."""
    files = row["content"]["files"]
    manifest = _yaml_from_files(files, "manifest.yaml")
    skill_id = manifest.get("id") or row["skill_id"]
    catalogue = _yaml_from_files(files, "catalogue.yaml")
    tests = catalogue.get("tests") or _yaml_from_files(files, "plan.yaml").get("tests", [])

    runs_rows = ctx.persistence.list_runs(filters={"skill_id": skill_id})
    completed = [r for r in runs_rows if r.get("status") == "completed"]
    last_run = runs_rows[0]["created_at"] if runs_rows else None

    return {
        "skill_id": skill_id,
        "name": manifest.get("name"),
        "domain": manifest.get("domain"),
        "category": manifest.get("category", manifest.get("domain")),
        "version": manifest.get("version"),
        "owner": manifest.get("owner"),
        "status": str(manifest.get("status", "draft")).replace("_", " ").title(),
        "last_updated": row.get("created_at"),
        "last_run": last_run,
        "description": manifest.get("description"),
        "tests": len(tests),
        "previous_runs": len(completed),
        "has_workspace": False,
        "origin": "explorer_saved",
        "catalogue_tests": tests,
        "contract_sources": sorted(_yaml_from_files(files, "contract.yaml").get("sources", {}).keys()),
    }


def _get_ledger_skill(ctx: AppContext, skill_id: str) -> dict | None:
    # P3/P4 perf gap review 2026-09-25 (/skills/<id> cold-load latency
    # pass): _ledger_skill_card issues its OWN ctx.persistence.list_runs
    # call per row -- this used to build a full card for every explorer_saved
    # skill just to check its id, one runs query per candidate, when at most
    # one row can ever match (the manifest's own "id" field is always set to
    # the SAME value as the row's skill_id column at save time,
    # save_explorer_draft_skill above) -- pre-filter on that column first,
    # so only the one matching row (if any) pays _ledger_skill_card's cost.
    for row in ctx.persistence.list_skill_versions_by_origin("explorer_saved"):
        if row["skill_id"] != skill_id:
            continue
        card = _ledger_skill_card(ctx, row)
        if card["skill_id"] == skill_id:
            return card
    return None


def list_skills(
    ctx: AppContext, *, runs: list[dict] | None = None, versions: list[dict] | None = None,
) -> list[dict]:
    # P3/P4 perf gap review 2026-09-25: this used to call
    # ctx.persistence.list_runs(filters={"skill_id": skill_id}) ONCE PER
    # SKILL DIRECTORY -- an N+1 that re-scans the whole runs/run_state join
    # from scratch for every skill, measured live against the real
    # warehouse at 17.06s for a single skill directory (dominated by that
    # one filtered list_runs call, whose cost is the same round trip as the
    # unfiltered one -- filtering narrows the WHERE clause, not the number
    # of round trips). `service.list_runs()` (below) then compounded this
    # further: it already fetches every run once for its own page, and used
    # to call this function -- paying the N+1 all over again -- to build its
    # skill-name lookup. `runs`, when given, is that already-fetched list
    # (never re-queried); every other caller (skill_library_page,
    # home_layout) still gets exactly ONE unfiltered list_runs() call here,
    # grouped by skill_id in Python, instead of one call per skill.
    all_runs = ctx.persistence.list_runs() if runs is None else runs
    runs_by_skill: dict[str, list[dict]] = {}
    for r in all_runs:
        runs_by_skill.setdefault(r.get("skill_id"), []).append(r)

    # P3/P4 perf gap review 2026-09-25 (/skills, /skills/<id> and
    # /workspace/tne cold-load latency pass): this used to call
    # ctx.persistence.list_skill_versions(skill_id) ONCE PER SKILL DIRECTORY
    # in the loop below, THEN a second, separate full-table scan
    # (list_skill_versions_by_origin) for the explorer_saved cards -- N+1
    # round trips against a table list_all_skill_versions already reads in
    # full (its own docstring: unavoidably a full scan at this table's
    # scale). One read, grouped by skill_id in Python, covers both uses:
    # `last_updated` per repo skill (below, sorted by version the same way
    # list_skill_versions(skill_id)'s own `ORDER BY version` would) and the
    # explorer_saved cards (same "last write wins in created_at order" rule
    # list_skill_versions_by_origin applied, since list_all_skill_versions
    # is ordered by skill_id, created_at -- identical result, one call).
    #
    # `versions`, like `runs` above, lets a caller that already fetched this
    # (get_actions_page_data, list_runs' own parallel fan-out below) hand it
    # straight in rather than paying for it twice -- BUG-ACTIONS-3, P3/P4
    # perf gap review 2026-09-25 live pass: this single read was measured
    # against the real warehouse at 0.9-3.2s, and /actions used to trigger
    # it via TWO separate, un-shared list_skills(ctx) calls.
    all_versions = ctx.persistence.list_all_skill_versions() if versions is None else versions
    versions_by_skill: dict[str, list[dict]] = {}
    for v in all_versions:
        versions_by_skill.setdefault(v["skill_id"], []).append(v)

    out: list[dict] = []
    for d in sorted(ctx.skills_dir.iterdir()):
        if not d.is_dir():
            continue
        manifest = _read_yaml(d / "manifest.yaml")
        skill_id = manifest.get("id")
        if not skill_id:
            continue

        catalogue = _read_yaml(d / "catalogue.yaml")
        if catalogue.get("tests"):
            n_tests = len(catalogue["tests"])
        else:
            n_tests = len(_read_yaml(d / "plan.yaml").get("tests", []))

        runs_rows = runs_by_skill.get(skill_id, [])
        completed = [r for r in runs_rows if r.get("status") == "completed"]
        last_run = runs_rows[0]["created_at"] if runs_rows else None

        versions = sorted(versions_by_skill.get(skill_id, []), key=lambda v: v["version"])
        last_updated = versions[-1]["created_at"] if versions else None

        out.append(
            {
                "skill_id": skill_id,
                "name": manifest.get("name"),
                "domain": manifest.get("domain"),
                "category": manifest.get("category", manifest.get("domain")),
                "version": manifest.get("version"),
                "owner": manifest.get("owner"),
                "status": str(manifest.get("status", "draft")).replace("_", " ").title(),
                "last_updated": last_updated,
                "last_run": last_run,
                "description": manifest.get("description"),
                "tests": n_tests,
                "previous_runs": len(completed),
                "has_workspace": (d / "workspace.py").is_file(),
            }
        )

    repo_ids = {e["skill_id"] for e in out}
    latest_explorer_saved: dict[str, dict] = {}
    for v in all_versions:  # ordered by skill_id, created_at asc -- last write wins
        if v["content"].get("origin") != "explorer_saved":
            continue
        latest_explorer_saved[v["skill_id"]] = v
    for row in latest_explorer_saved.values():
        card = _ledger_skill_card(ctx, row)
        if card["skill_id"] not in repo_ids:
            out.append(card)
    return out


# _plan_test_entries used to read only each plan test's top-level `flag`
# field, missing a primitive's other flag_* params (split_detection's
# flag_window, duplicate_detection's flag_oop_card) -- fixed once, shared
# with orchestrator.nodes.fieldwork.prioritise's exposure de-duplication,
# in orchestrator.skills.plan_test_flags (CLAUDE.md P2/P3 gate review item 5).
_plan_test_entries = plan_test_flags


def _catalogue_id_for_plan_test(plan_test_id: str, catalogue_ids: set[str]) -> str:
    # plan.yaml instantiates one catalogue test ("T3.3a") as several plan
    # tests ("T3.3a_dom", "T3.3a_int", "T3.3a_very_late") -- never fuzzy:
    # only an exact id match or an exact "<catalogue_id>_" prefix counts.
    if plan_test_id in catalogue_ids:
        return plan_test_id
    for cid in catalogue_ids:
        if plan_test_id.startswith(f"{cid}_"):
            return cid
    return plan_test_id


def get_skill(ctx: AppContext, skill_id: str) -> dict | None:
    try:
        d = _skill_dir_for(ctx, skill_id)
    except ValueError:
        return _get_ledger_skill(ctx, skill_id)
    # P3/P4 perf gap review 2026-09-25, live pass (/skills/<id> cold-load
    # pass): skill_methodology_page() (app/src/platform/methodology.py)
    # used to call get_skill(skill_id) AND list_skill_versions(skill_id)
    # separately -- the first already reads the WHOLE skill_versions table
    # once, inside list_skills(ctx) below, just to build `base`; the second
    # then read it AGAIN, filtered server-side instead of in Python, for
    # the exact same table (measured live against the real warehouse:
    # 0.9-3.2s for either query). Read once here and expose it as
    # `version_history_rows`, so a caller building the Version History tab
    # never has to issue a second read -- filtering an already-fetched list
    # in Python instead of a second round trip.
    all_versions = ctx.persistence.list_all_skill_versions()
    base = next((e for e in list_skills(ctx, versions=all_versions) if e["skill_id"] == skill_id), None)
    if base is None:
        return None
    version_history_rows = sorted(
        (v for v in all_versions if v["skill_id"] == skill_id), key=lambda v: v.get("version") or ""
    )

    contract = _read_yaml(d / "contract.yaml")
    sources = [
        {
            "source": name,
            "columns": list((cfg or {}).get("columns", {}).keys()),
            "file": (cfg or {}).get("file"),
            "sheet": (cfg or {}).get("sheet"),
        }
        for name, cfg in contract.get("sources", {}).items()
    ]
    catalogue = _read_yaml(d / "catalogue.yaml")
    tests = catalogue.get("tests") or _read_yaml(d / "plan.yaml").get("tests", [])

    # plan_tests / flag_to_test (drill-down mapping for the UI, CLAUDE.md
    # build brief P3 §1): every catalogue test entry gains the plan.yaml
    # primitive instance(s) -- and their RF_* flag(s) -- it actually resolves
    # to, and flag_to_test gives the reverse lookup (a flagged_rows.flag ->
    # the plan test that produced it) at the top level.
    plan_tests_raw = _read_yaml(d / "plan.yaml").get("tests", [])
    plan_entries = _plan_test_entries(plan_tests_raw)
    catalogue_ids = {t["test_id"] for t in tests if t.get("test_id")}
    plan_tests_by_catalogue: dict[str, list[dict]] = {}
    for e in plan_entries:
        cid = _catalogue_id_for_plan_test(e["test_id"], catalogue_ids)
        plan_tests_by_catalogue.setdefault(cid, []).append(e)

    # Render each catalogue entry's `threshold` template against thresholds.yaml
    # (CLAUDE.md §0.4/G8, P2/P3 gate review item 4): catalogue.yaml deliberately
    # carries `{threshold_id}` placeholders rather than a hand-typed number so
    # G8 (tests/test_g8_thresholds.py) can assert the catalogue and
    # thresholds.yaml never disagree BY CONSTRUCTION -- but a caller reading
    # the raw template (methodology.py, workspace_tne.py's catalogue tab) would
    # show literal "{...}" braces unless it is resolved once, here, the same
    # way test_g8_thresholds.py does. `threshold_provenance` names every
    # threshold id the rendered text drew on, so the UI can label a test
    # whose threshold is analyst-set / pending_policy_confirmation without
    # re-parsing the template itself.
    thresholds = _read_yaml(d / "thresholds.yaml")
    threshold_values = {tid: spec.get("value") for tid, spec in thresholds.items()}
    fmt = string.Formatter()

    def _render_threshold(entry: dict) -> dict:
        raw = entry.get("threshold")
        if not isinstance(raw, str):
            return entry
        used_ids = [fn for _, fn, _, _ in fmt.parse(raw) if fn]
        try:
            rendered = raw.format(**threshold_values)
        except (KeyError, IndexError):
            return entry  # an id referenced isn't in thresholds.yaml -- leave the raw template, don't fabricate a number
        return {
            **entry,
            "threshold": rendered,
            "threshold_provenance": [
                {"id": tid, **(thresholds.get(tid, {}).get("provenance") or {})} for tid in used_ids
            ],
        }

    tests_with_plan = [
        {**_render_threshold(t), "plan_tests": plan_tests_by_catalogue.get(t.get("test_id"), [])} for t in tests
    ]
    flag_to_test = {e["flag"]: e["test_id"] for e in plan_entries}

    risk_control = _read_yaml(d / "risk_control.yaml")

    return {
        **base, "sources": sources, "tests": tests_with_plan, "flag_to_test": flag_to_test,
        "thresholds": thresholds, "risk_control": risk_control,
        "version_history_rows": version_history_rows,
    }


def list_skill_versions(ctx: AppContext, skill_id: str) -> list[dict]:
    """Recorded skill_versions rows for this skill_id, newest last (CLAUDE.md
    §4.6/§8: an immutable content-hashed snapshot per publish). Empty until a
    Skill has actually been published/confirmed through record_skill_version
    -- the UI (methodology page) must show that explicitly, never fabricate
    version-history entries (CLAUDE.md P2/P3 gate review item 4)."""
    return ctx.persistence.list_skill_versions(skill_id)


def get_skill_version_plan(ctx: AppContext, skill_id: str, version: str) -> dict | None:
    """The `plan.yaml` content this SPECIFIC skill_id/version actually ran
    with, read from its immutable `skill_versions` row (CLAUDE.md §4.6/§8:
    the content-hashed snapshot register_skill recorded when this version
    was first confirmed) -- never the live skills/<id>/plan.yaml on disk,
    which may have moved on since this run executed (independent review
    2026-09-24 item 7). Returns None only when no such version was ever
    registered (an older run, or a Skill not yet promoted through the
    registry) -- callers fall back to the live directory for THAT case
    only, never to paper over a genuine read/parse failure."""
    row = ctx.persistence.get_skill_version(skill_id, version)
    if row is None:
        return None
    return row["content"].get("plan")


# ── Governed data discovery ───────────────────────────────────────────────────


def list_governed_tables(ctx: AppContext) -> list[dict]:
    """One entry per discoverable table, in the SAME shape regardless of
    backend -- {fqn, catalog, schema, table, comment, columns} -- mirroring
    UCTableDataSource.list_tables() (CLAUDE.md §8 P5's "no-access catalogs
    surface as Restricted": a catalog/schema this identity cannot list comes
    back as {"fqn": ..., "restricted": True} there, and local mode has no
    such notion so it never emits one)."""
    if ctx.backend == "local":
        configs = _all_skill_source_configs(ctx.skills_dir)
        root = ctx.local_data_root or Path(".")
        out = []
        for name, cfg in sorted(configs.items()):
            file_name = cfg.get("file", name)
            file_path = Path(root) / file_name
            entry = {
                "fqn": file_name,
                "catalog": None,
                "schema": None,
                "table": name,
                "comment": "Local test data" if file_path.is_file() else "File not found",
                "columns": list((cfg or {}).get("columns", {}).keys()),
            }
            # A local source has no UC owner and no row count worth
            # computing here (CLAUDE.md §5 UI item 3: "owner omitted" for
            # files) -- but it does have a real filesystem mtime, so
            # "Refreshed" need not render the literal None either.
            if file_path.is_file():
                import datetime

                entry["last_refreshed"] = datetime.date.fromtimestamp(file_path.stat().st_mtime).isoformat()
            out.append(entry)
        return out

    data_source = ctx.data_source_factory({})
    try:
        list_tables = getattr(data_source, "list_tables", None)
        if list_tables is None:
            from orchestrator.errors import ConfigError

            raise ConfigError(
                "UCTableDataSource.list_tables() is not available -- the Unity Catalog source-data "
                "work has not landed in this checkout yet"
            )
        return list_tables()
    finally:
        _close_data_source(data_source)


def list_data_asset_cards(ctx: AppContext, query: str = "", limit: int | None = None) -> list[dict]:
    """The data_asset_card component's shape, filtered by `query` the same
    way the UI's search box always has (fully-qualified name / catalog /
    schema / table / comment), and never carrying a literal None for a field
    the source cannot supply (CLAUDE.md §5 UI item 3): a key is present only
    when there is a real value, so components.data_asset_card's own existing
    "—" / "File" fallbacks render instead of the word "None".

    Row count and UC tag classification are real per-table queries -- too
    expensive to run for a whole catalog listing, so they are fetched only
    for `limit` entries (the cards a caller is actually about to render),
    after filtering. Omit `limit` to shape every match with no row count or
    classification lookups at all (e.g. a caller that filters further
    itself)."""
    tables = list_governed_tables(ctx)
    if query:
        q = query.lower()
        tables = [
            t for t in tables
            if q in (t.get("fqn") or "").lower()
            or q in (t.get("catalog") or "").lower()
            or q in (t.get("schema") or "").lower()
            or q in (t.get("table") or "").lower()
            or q in (t.get("comment") or "").lower()
        ]
    if limit is not None:
        tables = tables[:limit]

    data_source = ctx.data_source_factory({}) if ctx.backend != "local" else None

    try:
        cards = []
        lookup_targets: list[tuple[str, dict]] = []
        for t in tables:
            card = {
                "name": t.get("fqn") or t.get("table") or "",
                "type": "Table",
                "description": t.get("comment") or "",
                "access": "Restricted" if t.get("restricted") else "Available",
            }
            if t.get("owner"):
                card["owner"] = t["owner"]
            if t.get("last_refreshed"):
                card["last_refreshed"] = t["last_refreshed"]
            cards.append(card)
            if limit is not None and data_source is not None and not t.get("restricted") and t.get("fqn"):
                lookup_targets.append((t["fqn"], card))

        # P3/P4 perf gap review 2026-09-25, live pass: each table's row-count
        # + classification lookup is independent of every other table's --
        # measured live against the real warehouse at several hundred ms to
        # ~1s per call (a DESCRIBE HISTORY + a COUNT, then a tag lookup),
        # run one table after another that was N tables x 2 calls each in
        # series. The SHARED UC connection pool (AppContext.uc_pool) is
        # exactly what lets this fan out safely -- multiple threads
        # checking connections in and out of the same bounded pool is what
        # it exists for; `data_source` (a UCTableDataSource) caches nothing
        # per-call that isn't safe under that (the module-level row-count
        # cache, the WorkspaceClient lock).
        def _fill(fqn: str, card: dict) -> None:
            card["rows"] = data_source.get_row_count(fqn)
            classification = data_source.get_classification(fqn)
            if classification:
                card["classification"] = classification

        if len(lookup_targets) > 1:
            from concurrent.futures import ThreadPoolExecutor

            max_workers = min(
                len(lookup_targets), max(1, getattr(ctx.settings, "max_connections", len(lookup_targets)))
            )
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = [pool.submit(_fill, fqn, card) for fqn, card in lookup_targets]
                for f in futures:
                    f.result()
        else:
            for fqn, card in lookup_targets:
                _fill(fqn, card)

        return cards
    finally:
        if data_source is not None:
            _close_data_source(data_source)


def suggest_bindings(ctx: AppContext, skill_id: str, *, skill: dict | None = None) -> dict[str, str | None]:
    """`skill`, when given, is an already-fetched `get_skill(ctx, skill_id)`
    result -- skips this function's own call. P3/P4 perf gap review
    2026-09-25, live pass: app/src/run_setup.py's `_auto_bind` (the
    synchronous portion of "Start audit analysis", CLAUDE.md §11 "Run
    start opens the run page at once") used to call `adapters.get_skill`
    directly AND `adapters.suggest_bindings`, which called `get_skill`
    AGAIN internally -- two skill_versions + list_runs round trips for the
    one skill card this whole function ever needed (BUG-STARTRUN-1;
    measured live at ~0.9-1.3s for the skill_versions read alone)."""
    skill = get_skill(ctx, skill_id) if skill is None else skill
    if skill is None:
        return {}
    source_names = [s["source"] for s in skill["sources"]]

    # Independent review 2026-09-24 item 1: a source with an explicit
    # SOURCE_BINDINGS entry (a Volume file's exact path, or a governed
    # table's exact FQN) is auto-bound to it directly -- no new UI needed,
    # and no guessing: this is the one case that beats every other
    # auto-bind rule below, because it was declared by name, not matched.
    configured = suggested_values(configured_bindings_for_skill(ctx, skill_id))

    if ctx.backend == "local":
        configs = _all_skill_source_configs(ctx.skills_dir)
        return {
            name: configured.get(name) or (configs.get(name) or {}).get("file")
            for name in source_names
        }

    tables = list_governed_tables(ctx)
    by_short_name: dict[str, str] = {}
    for t in tables:
        if t.get("restricted"):
            continue
        short = t.get("table")
        fqn = t.get("fqn")
        if not short or not fqn:
            continue
        # Never fuzzy: only an EXACT match of the table's own short name to the
        # contract source name is suggested; the first one found wins, later
        # duplicates are left alone rather than overriding it.
        by_short_name.setdefault(short, fqn)
    return {name: configured.get(name) or by_short_name.get(name) for name in source_names}


# ── Runs ──────────────────────────────────────────────────────────────────────


def generate_run_id() -> str:
    """CLAUDE.md §11 "Run start opens the run page at once" (2026-09-25):
    the one place app/src/platform/adapters.py may generate a run_id before
    a run exists -- a thin re-export of runs_module.generate_run_id() so the
    web tier's pre-generated id and start_audit_run's own default (when no
    run_id is supplied) are always produced by the exact same function."""
    return runs_module.generate_run_id()


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
    business_unit: str | None = None,
    materiality: float | None = None,
    generate_management_actions: bool = True,
    jira_preview_requested: bool = False,
    run_id: str | None = None,
) -> str:
    # `run_id`, when given (CLAUDE.md §11 "Run start opens the run page at
    # once", 2026-09-25): the caller (app/src/run_setup.py's async-start
    # worker) already pre-generated it via generate_run_id() above and
    # navigated the browser to /run/<run_id> before this function even
    # started running. Validated HERE, against generate_run_id()'s own
    # format, before it becomes a Delta row's identity (NN14) -- never
    # trusted blind, since it crossed the web-tier boundary. This is
    # deliberately NOT enforced inside runs_module.create_run itself: every
    # other internal caller (tests/, other orchestrator/ code) passes its
    # own human-readable run_id for fixture clarity, which is a different,
    # already-trusted caller this check must not break. None (the default,
    # every other caller of THIS function) keeps today's behaviour of
    # generating one inside create_run.
    if run_id is not None and not runs_module.is_valid_run_id(run_id):
        raise InvalidRunId(run_id)

    # Independent review 2026-09-24 item 5: a run refuses to start while a
    # required readiness check is failing -- the same cached report /ready
    # exposes, not a fresh probe on every click (cost -- CLAUDE.md §11 idle-
    # cost incident). Only checks that are actually required given the
    # current configuration block the run (e.g. the model endpoints, which
    # no fieldwork node calls unless `enable_row_level_llm` is on --
    # independent-review follow-up item 2); RunNotReady's message names
    # only the sanitized check names, never raw internal exception text;
    # app/src/run_setup.py's existing generic "Could not start the run:
    # {exc}" handler in the run-summary-preview panel already shows it.
    if ctx.readiness is not None:
        report = ctx.readiness.get()
        blocking = report.blocking_failures()
        if blocking:
            raise RunNotReady([c.name for c in blocking])

    # load_skill_by_id (docs/specs/P6_P8_explorer_llm_design.md §4.13 point
    # 5): a repo Skill directory as before, or -- for a Playbook run of a
    # saved Explorer draft -- its skill_versions ledger row.
    skill = load_skill_by_id(ctx, skill_id)
    skill.validate()
    skill_dir = skill.skill_dir

    contract_sources = skill.contract.get("sources", {})
    missing = sorted(set(contract_sources) - set(bindings))
    if missing:
        raise ContractViolation([f"no binding supplied for contract source {s!r}" for s in missing])

    data_source = ctx.data_source_factory(bindings, contract_sources, skill_id)

    # Resolve every source's version FIRST, before any read (CLAUDE.md §4.1
    # TOCTOU ordering) -- these become both this run's pinned data_assets
    # bindings and the fingerprint's source_table_versions.
    #
    # P3/P4 perf gap review (2026-09-25): resolve_source_versions (not a
    # per-source resolve_version() loop) lets a UC-backed/Volume-aware data
    # source resolve all of a Skill's sources concurrently, on a bounded pool
    # of connections, instead of one DESCRIBE HISTORY/file-hash at a time --
    # live measurement showed SKILL-001's 8 sources taking 13.0s resolved
    # sequentially. Still resolved before any read either way; this only
    # changes how the resolutions themselves run.
    try:
        source_versions = data_source.resolve_source_versions(list(contract_sources))
    finally:
        # This resolve-only data source is never reused past this point
        # (a fresh instance is built for the run's own executor pass, via
        # NodeContext) -- closed here rather than left for GC (BUG-UCPOOL-1
        # belt-and-braces).
        _close_data_source(data_source)

    # A source bound to an uploaded file (run_setup._auto_bind's exact-
    # filename-stem match), or to a SOURCE_BINDINGS-configured Volume file
    # (independent review 2026-09-24 item 1), is pinned in
    # run_fingerprints.uploaded_file_hashes ({volume_path: sha256},
    # CLAUDE.md §4.1 P1A) as well as in source_table_versions above -- the
    # fingerprint records not just WHAT version was read but that it came
    # from a flat file, not a governed table.
    uploaded_file_hashes = _flat_file_hashes(
        ctx, skill_id, {bindings[name]: source_versions[name] for name in contract_sources}
    )

    now = ctx.clock()
    fingerprint = _compute_run_fingerprint(ctx, skill_dir, source_versions, uploaded_file_hashes)

    _ensure_skill_registered(ctx, skill, run_owner, now)

    # data_assets is NODE_OWNED (the `discover` node's field), but bindings
    # must already be present for `discover` to validate against -- exactly
    # like a resolved plan being handed to `execute`, the service establishes
    # them once at creation, before any node has run, which the pipeline's
    # per-node lifecycle check never sees or restricts (CLAUDE.md build brief
    # P3 §2, discover node docstring). Passed into create_run itself, part of
    # the SAME initial insert, rather than a follow-up save_state: the run
    # becomes visible to any already-running executor's admission loop
    # (find_runs(["queued"])) the instant the row lands, and a second write
    # here racing that executor's own first CAS transition would lose --
    # StaleStateError, observed live against a real deployed App.
    data_assets = [
        {"source": name, "table_fqn": bindings[name], "version": source_versions[name]}
        for name in contract_sources
    ]

    # generate_management_actions/jira_preview_requested are recorded here,
    # real and visible on the run (RunState.options, never fabricated), but
    # are NOT YET wired to gate the `act` node or export's Jira-preview step
    # -- that is pipeline-node work this change does not make (see the UI
    # task's report). Recording them now means no run ever silently drops
    # what the auditor asked for; it is simply not enforced yet.
    options = {
        "auto_confirm_plan": not review_plan_first,
        "generate_management_actions": generate_management_actions,
        "jira_preview_requested": jira_preview_requested,
    }
    state = runs_module.create_run(
        ctx.persistence,
        run_id=run_id,
        run_kind="fieldwork",
        engagement_id=engagement_id,
        skill_id=skill_id,
        skill_version=skill.version,
        mode=mode,
        audit_period=tuple(audit_period),
        objective=objective,
        run_owner=run_owner,
        business_unit=business_unit,
        materiality=materiality,
        options=options,
        data_assets=data_assets,
        fingerprint=fingerprint,
        now=now,
    )

    if ctx.executor is not None:
        ctx.executor.start(state.run_id, state.phase)

    return state.run_id


def _explorer_source_entries(sources: list[dict]) -> list[dict]:
    """docs/specs/P6_P8_explorer_llm_design.md §4.2: `{"name", "kind", "ref",
    "format", "file"}` per given `{"kind", "ref"}`, names derived
    deterministically from `ref` and de-duplicated in input order. `format`
    is inferred from `ref`'s own extension for a `local_file` source when
    the caller did not give one -- `ref` really is a file path/name for that
    kind. For `upload`, `ref` is an opaque upload_id (BUG-3: it has no
    extension to infer from), so `format` is left unset here unless the
    caller gave one explicitly; `_resolve_explorer_upload_entries` fills it
    in from the uploaded_files row once it is looked up. A `uc_table` source
    has no format at all."""
    taken: set[str] = set()
    out: list[dict] = []
    for s in sources:
        kind = s.get("kind")
        ref = s.get("ref")
        if kind not in ("uc_table", "upload", "local_file"):
            raise ExplorerInputError(f"start_explorer_run: unsupported source kind {kind!r}")
        if not ref:
            raise ExplorerInputError("start_explorer_run: every source needs a non-empty ref")
        name = _explorer_source_name(ref, taken)
        entry: dict = {"name": name, "kind": kind, "ref": ref}
        if kind == "local_file":
            entry["format"] = s.get("format") or _infer_source_format(ref)
        elif kind == "upload" and s.get("format"):
            entry["format"] = s["format"]
        if s.get("file"):
            entry["file"] = s["file"]
        out.append(entry)
    return out


def _resolve_explorer_upload_entries(
    ctx: AppContext, entries: list[dict],
    table_fqn_by_name: dict[str, str], version_by_name: dict[str, str], uploaded_file_hashes: dict[str, dict],
) -> None:
    """Resolves every `upload`-kind entry the SAME way regardless of
    backend: an `upload` source's `ref` is an upload_id, never a file path
    -- `table_fqn` is the upload's OWN recorded volume_path (so
    `_build_explorer_data_source`'s local LocalFileDataSource / non-local
    VolumeUploadAwareDataSource wrapping recognises it the same way a
    Playbook upload binding does) and `version` is the sha256 already
    recorded at upload time -- read again, never trusted blind, the next
    time `_build_explorer_data_source` actually reads it. Mutates the three
    dicts in place; a non-Ready or unknown upload_id fails loudly (NN14),
    never silently skipped.

    BUG-3 fix: also resolves `e["format"]` here (when the caller did not
    give one explicitly in `_explorer_source_entries`), from the upload's
    own recorded filename -- never from the upload_id `ref`, which has no
    extension. This is the format `_build_explorer_data_source`'s local
    branch and `options.explorer.sources` (start_explorer_run below) both
    read; leaving it unresolved silently defaulted every uploaded file to
    "csv" on the local backend regardless of its real format."""
    for e in entries:
        if e["kind"] != "upload":
            continue
        row = ctx.persistence.get_uploaded_file(e["ref"])
        if row is None or row.get("status") != "Ready":
            raise ExplorerInputError(
                f"start_explorer_run: upload {e['ref']!r} is not a Ready uploaded file"
            )
        table_fqn_by_name[e["name"]] = row["volume_path"]
        version_by_name[e["name"]] = row["sha256"]
        uploaded_file_hashes[row["volume_path"]] = row["sha256"]
        if not e.get("format"):
            e["format"] = _infer_source_format(row.get("filename") or row["volume_path"])


def _resolve_explorer_sources(
    ctx: AppContext, entries: list[dict]
) -> tuple[dict[str, str], dict[str, str], dict[str, dict]]:
    """Resolves every Explorer source's version BEFORE any read (CLAUDE.md
    §4.1 TOCTOU ordering -- the same discipline start_audit_run's own
    source_versions resolution follows). Returns `(table_fqn_by_name,
    version_by_name, uploaded_file_hashes)`."""
    table_fqn_by_name: dict[str, str] = {}
    version_by_name: dict[str, str] = {}
    uploaded_file_hashes: dict[str, str] = {}

    if ctx.backend == "local":
        # An `upload`-kind entry is resolved via
        # _resolve_explorer_upload_entries, exactly like the non-local
        # branch below -- never through LocalFileDataSource, whose
        # `root_dir` is ctx.local_data_root and has no relationship to
        # where an upload's own volume_path lives. Only non-upload entries
        # (local_file) are files under local_data_root.
        local_entries = [e for e in entries if e["kind"] != "upload"]
        local_sources = {
            e["name"]: {"format": e.get("format") or "csv", "file": e.get("file") or e["ref"]}
            for e in local_entries
        }
        data_source = LocalFileDataSource(root_dir=ctx.local_data_root, sources=local_sources)
        for e in local_entries:
            table_fqn_by_name[e["name"]] = e["ref"]
            version_by_name[e["name"]] = data_source.resolve_version(e["name"])
        _resolve_explorer_upload_entries(ctx, entries, table_fqn_by_name, version_by_name, uploaded_file_hashes)
        return table_fqn_by_name, version_by_name, uploaded_file_hashes

    uc_names = [e for e in entries if e["kind"] == "uc_table"]
    if uc_names:
        from orchestrator.adapters.datasource_uc import UCTableDataSource

        uc_bindings = {e["name"]: e["ref"] for e in uc_names}
        uc_source = UCTableDataSource(ctx.settings, uc_bindings)
        try:
            for e in uc_names:
                table_fqn_by_name[e["name"]] = e["ref"]
                version_by_name[e["name"]] = uc_source.resolve_version(e["name"])
        finally:
            _close_data_source(uc_source)

    _resolve_explorer_upload_entries(ctx, entries, table_fqn_by_name, version_by_name, uploaded_file_hashes)

    if any(e["kind"] == "local_file" for e in entries):
        raise ExplorerInputError(
            "start_explorer_run: 'local_file' sources are only supported on the local backend"
        )

    return table_fqn_by_name, version_by_name, uploaded_file_hashes


def start_explorer_run(
    ctx: AppContext,
    *,
    objective: str,
    sources: list[dict],
    audit_period: tuple[str, str],
    run_owner: str,
    engagement_id: str = "ENG-DEFAULT",
    business_unit: str | None = None,
    materiality: float | None = None,
    generate_management_actions: bool = True,
    jira_preview_requested: bool = False,
    supersedes_run_id: str | None = None,
    reference_skill_ids: list[str] | None = None,
) -> str:
    """docs/specs/P6_P8_explorer_llm_design.md §4.2: creates an Explorer run
    (mode='explorer', skill_id=None, phase='plan') from 1-5 selected data
    sources and an auditor's objective. `auto_confirm_plan` is always False
    -- plan confirmation is mandatory in Explorer (§2.4), never the
    Playbook `review_plan_first`-driven default."""
    if ctx.readiness is not None:
        report = ctx.readiness.get()
        blocking = report.blocking_failures()
        if blocking:
            raise RunNotReady([c.name for c in blocking])

    if not (1 <= len(sources) <= 5):
        raise ExplorerInputError(
            f"start_explorer_run: sources must have 1 to 5 entries, got {len(sources)}"
        )
    if not objective or not objective.strip():
        raise ExplorerInputError("start_explorer_run: objective must be non-empty")
    if len(objective) > 4000:
        raise ExplorerInputError("start_explorer_run: objective exceeds 4000 characters")
    audit_timezone = getattr(ctx.settings, "audit_timezone", None)
    if not audit_timezone:
        raise ConfigError("start_explorer_run: settings.audit_timezone is unset (CLAUDE.md NN14)")

    now = ctx.clock()

    if supersedes_run_id:
        prior = ctx.persistence.load_state(supersedes_run_id)
        if prior.mode == "explorer" and prior.status == "awaiting_confirmation":
            runs_module.reject(
                ctx.persistence, supersedes_run_id, actor=run_owner,
                reason=f"Superseded by a new Explorer objective (see run started at {now})",
                now=now,
            )

    entries = _explorer_source_entries(sources)
    table_fqn_by_name, version_by_name, uploaded_file_hashes = _resolve_explorer_sources(ctx, entries)

    # BUG-EXPLORER-2 (independent review round 2): a caller that leaves
    # reference_skill_ids unset gets the config-driven default (§4.5),
    # resolved once here and recorded in options/the fingerprint below --
    # never re-resolved on a later executor pass, so which reference Skills
    # this run's planner saw stays reproducible even if the repo's Skill
    # list changes later.
    if reference_skill_ids is None:
        reference_skill_ids = _default_reference_skill_ids(ctx)

    fingerprint = compute_fingerprint(
        settings=ctx.settings, source_table_versions=dict(version_by_name),
        uploaded_file_hashes=uploaded_file_hashes, skill_dir=None,
        requirements_path=REPO_ROOT / "requirements.txt",
        prompts_dirs=[REPO_ROOT / "orchestrator" / "prompts" / "explorer"],
        reference_files=[], code_revision=None,
        skill_content_hash=_explorer_inputs_hash(ctx, reference_skill_ids or []),
    )

    options = {
        "auto_confirm_plan": False,
        "generate_management_actions": generate_management_actions,
        "jira_preview_requested": jira_preview_requested,
        "explorer": {
            "sources": [
                {
                    "name": e["name"], "kind": e["kind"], "ref": e["ref"],
                    "format": e.get("format"), "file": e.get("file"),
                }
                for e in entries
            ],
            "reference_skill_ids": list(reference_skill_ids or []),
            "audit_timezone": audit_timezone,
        },
    }
    data_assets = [
        {"source": e["name"], "table_fqn": table_fqn_by_name[e["name"]], "version": version_by_name[e["name"]],
         "kind": e["kind"]}
        for e in entries
    ]

    state = runs_module.create_run(
        ctx.persistence,
        run_kind="fieldwork",
        engagement_id=engagement_id,
        skill_id=None,
        skill_version=None,
        mode="explorer",
        audit_period=tuple(audit_period),
        objective=objective,
        run_owner=run_owner,
        business_unit=business_unit,
        materiality=materiality,
        options=options,
        data_assets=data_assets,
        fingerprint=fingerprint,
        now=now,
    )

    if ctx.executor is not None:
        ctx.executor.start(state.run_id, state.phase)

    return state.run_id


def get_explorer_review(ctx: AppContext, run_id: str) -> dict:
    """docs/specs/P6_P8_explorer_llm_design.md §5.1 "get_explorer_review":
    the Explorer proposal in review-panel shape -- every proposed test with
    its validity, reasons, rationale and current included/excluded state
    (state.plan_edits applied), for the workflow-preview row-per-test
    rendering and for confirm_plan's own precondition check. Column/enum/
    threshold ALLOWED VALUES for the edit UI (§4.10's own "computed by
    Python" column) are not computed here yet -- D4's day-one edit surface
    is include/exclude only, and full param editing is deferred (§4.13/§10)."""
    state = ctx.persistence.load_state(run_id)
    plan = state.plan or {}
    proposal = plan.get("proposal")
    validation = plan.get("validation") or {"tests": {}, "findings": {}, "proposal_errors": [], "warnings": []}
    excluded = excluded_test_keys(state.plan_edits)

    tests: list[dict] = []
    for t in (proposal or {}).get("tests", []):
        key = t["key"]
        v = validation.get("tests", {}).get(key) or {"valid": False, "reasons": []}
        valid = bool(v.get("valid"))
        tests.append({
            "key": key,
            "name": t.get("name"),
            "primitive": t.get("primitive"),
            "control_objective": t.get("control_objective"),
            "risk_hypothesis": t.get("risk_hypothesis"),
            "rationale": state.plan_rationale.get(key, ""),
            "valid": valid,
            "reasons": v.get("reasons", []),
            "excluded": key in excluded,
            "included": valid and key not in excluded,
        })

    return {
        "run_id": run_id,
        "run_status": state.status,
        "plan_status": plan.get("status"),
        "llm_unavailable": plan.get("status") == "llm_unavailable",
        "label": plan.get("label"),
        "proposal_errors": validation.get("proposal_errors", []),
        "warnings": validation.get("warnings", []),
        "tests": tests,
        "n_valid": sum(1 for t in tests if t["valid"]),
        "n_total": len(tests),
        "n_included": sum(1 for t in tests if t["included"]),
        "data_gaps": plan.get("data_gaps_computed", []),
        "assumptions": (proposal or {}).get("assumptions", []),
        "summary": (proposal or {}).get("summary"),
        "edits": list(state.plan_edits),
        "confirmable": plan.get("status") == "proposed" and any(t["included"] for t in tests),
    }


def edit_explorer_plan(ctx: AppContext, run_id: str, edits: list[dict], actor: str) -> RunState:
    """docs/specs/P6_P8_explorer_llm_design.md §4.10: applies `edits` on top
    of `state.plan_edits`, revalidates the WHOLE resulting proposal, and
    refuses the batch outright (ExplorerEditRejected, nothing partial ever
    recorded) if any still-included test would come out invalid. Only on
    success are the edits appended -- each with its real before/after value
    (`_edit_before_value`) -- and a `plan_edited` trace event emitted."""
    now = ctx.clock()
    state = ctx.persistence.load_state(run_id)
    if state.mode != "explorer" or state.status != "awaiting_confirmation":
        raise ExplorerPlanNotConfirmable(
            f"run {run_id!r} is not an Explorer run awaiting_confirmation "
            f"(status={state.status!r}, mode={state.mode!r})"
        )
    plan = state.plan or {}
    proposal = plan.get("proposal")
    if plan.get("status") != "proposed" or proposal is None:
        raise ExplorerEditRejected([f"plan.status is {plan.get('status')!r}, not 'proposed' -- nothing to edit"])
    if not edits:
        return state

    candidate_edits = list(state.plan_edits) + [dict(e) for e in edits]
    effective = apply_plan_edits(proposal, candidate_edits)
    data_source = _build_explorer_data_source(ctx, state)
    sources = (state.profile_result or {}).get("sources", {})
    pinned_versions = {b["source"]: b["version"] for b in state.data_assets}
    try:
        report = validate_proposal(
            effective, profile=sources, run_sources=sorted(sources), data_source=data_source,
            pinned_versions=pinned_versions,
        )
    finally:
        _close_data_source(data_source)
    included_keys = {t["key"] for t in effective.get("tests", [])}
    invalid_included = sorted(key for key in included_keys if not report["tests"].get(key, {}).get("valid"))
    if invalid_included:
        reasons = [
            f"{key}: {r['rule']}: {r['message']}"
            for key in invalid_included
            for r in report["tests"].get(key, {}).get("reasons", [])
        ] or [f"edit would leave {invalid_included} invalid"]
        raise ExplorerEditRejected(reasons)

    recorded: list[dict] = []
    for i, edit in enumerate(edits):
        prior_edits = list(state.plan_edits) + edits[:i]
        record = dict(edit)
        record["before"] = _edit_before_value(proposal, prior_edits, edit)
        record["after"] = (
            edit["value"] if edit.get("op") in ("set_column", "set_enum", "set_threshold")
            else edit.get("op") == "include_test"
        )
        record["actor"] = actor
        record["at"] = now
        recorded.append(record)

    new_state = replace(state, plan_edits=list(state.plan_edits) + recorded)
    saved = ctx.persistence.save_state(new_state)
    message = "; ".join(_describe_edit(e) for e in recorded)
    runs_module._emit(ctx.persistence, saved, event_type="plan_edited", actor=actor, message=message, now=now)
    return saved


def save_explorer_draft_skill(ctx: AppContext, run_id: str, actor: str) -> dict:
    """docs/specs/P6_P8_explorer_llm_design.md §4.13: saves a COMPLETED,
    signed-off Explorer run's EXPLORER-<run_id> ledger Skill as a draft Skill
    a Playbook run can load by skill_id (origin='explorer_saved'). Rewrites
    only manifest.yaml (id/version/status/owner/description); every other
    file is carried over byte-for-byte. Idempotent: the SAME run saved twice
    re-derives the identical content_hash (the id itself is derived from it),
    so record_skill_version's own idempotency (CLAUDE.md §4.6/§8) returns
    the existing row rather than raising SkillVersionConflict."""
    now = ctx.clock()
    state = ctx.persistence.load_state(run_id)
    if state.mode != "explorer" or state.status != "completed" or state.signoff is None:
        raise ExplorerPlanNotConfirmable(
            f"run {run_id!r} is not a signed-off, completed Explorer run "
            f"(status={state.status!r}, signoff={'present' if state.signoff else 'absent'})"
        )
    row = ctx.persistence.get_skill_version(f"EXPLORER-{run_id}", "run")
    if row is None:
        raise PlanIntegrityError(f"run {run_id!r}: no EXPLORER-{run_id} ledger row to save a draft from")

    files = dict(row["content"]["files"])
    manifest = yaml.safe_load(files["manifest.yaml"]) or {}
    ns = row["content_hash"][:8]
    skill_id = f"SKILL-X-{ns}"
    description = (manifest.get("description") or "").rstrip()
    suffix = (
        f" Authored in Explorer Mode from run {run_id}; thresholds are analyst-set "
        f"pending policy confirmation."
    )
    manifest = {
        **manifest,
        "id": skill_id,
        "version": "0.1.0",
        "status": "draft",
        "owner": actor,
        "description": (description + suffix).strip(),
    }
    files["manifest.yaml"] = yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True)

    entries = [(name, text.encode("utf-8")) for name, text in files.items()]
    content_hash = hash_skill_content_entries(entries)
    new_content = {"files": files, "origin": "explorer_saved", "source_run_id": run_id}
    saved_row = ctx.persistence.record_skill_version(
        skill_id=skill_id, version="0.1.0", content_hash=content_hash, content=new_content,
        created_by=actor, now=now, status="draft",
    )

    message = f"Saved as draft Skill {skill_id} v0.1.0 by {actor}"
    event_id = hashlib.sha256(f"{run_id}|skill_draft_saved|{content_hash}".encode("utf-8")).hexdigest()[:32]
    ctx.persistence.append_trace_event({
        "event_id": event_id, "run_id": run_id, "engagement_id": state.engagement_id,
        "event_type": "skill_draft_saved", "event_time": now, "stage": "Skills", "status": state.status,
        "message": message, "duration_s": None, "node_name": None, "execution_key": None,
        "actor": actor, "state_version": state.state_version,
    })
    return {"skill_id": skill_id, "version": "0.1.0", "content_hash": saved_row["content_hash"]}


_SURFACE2_MIN_PRECISION = 0.98
_SURFACE2_MIN_RECALL = 0.95


def _surface2_all_pass(results: Any) -> bool:
    if isinstance(results, str):
        try:
            results = json.loads(results)
        except (TypeError, ValueError):
            return False
    if not isinstance(results, dict) or not results:
        return False
    for score in results.values():
        if not isinstance(score, dict):
            return False
        precision, recall = score.get("precision"), score.get("recall")
        if precision is None or recall is None:
            return False
        if precision < _SURFACE2_MIN_PRECISION or recall < _SURFACE2_MIN_RECALL:
            return False
    return True


def publish_skill(ctx: AppContext, skill_id: str, version: str, reviewer: str | None) -> dict:
    """docs/specs/P6_P8_explorer_llm_design.md §4.13 "Promotion draft ->
    published": a GUARD ONLY -- no publish UI or persistence write-through
    exists yet (a planted-fixture generator for arbitrary Explorer
    contracts, the prerequisite for any Explorer draft to ever earn a
    passing Surface 2 result, is P8/P9 follow-up work, §10). Raises
    PromotionRequirementsNotMet naming every unmet requirement; a future
    caller wires the actual status transition once that generator exists."""
    row = ctx.persistence.get_skill_version(skill_id, version)
    if row is None:
        raise PromotionRequirementsNotMet([f"no skill_versions row for ({skill_id!r}, {version!r})"])

    missing: list[str] = []
    if row.get("status") != "draft":
        missing.append(f"status is {row.get('status')!r}, not 'draft'")
    if not reviewer or reviewer == row.get("created_by"):
        missing.append("a named reviewer distinct from created_by is required")
    if not _surface2_all_pass(row.get("surface2_results_json")):
        missing.append(
            "Surface 2 results (precision >= 0.98, recall >= 0.95 per test) are not recorded for this version"
        )
    if missing:
        raise PromotionRequirementsNotMet(missing)
    return {"skill_id": skill_id, "version": version, "status": "requirements_met", "reviewed_by": reviewer}


def _queue_affinity_note_from_fingerprint(
    status: str, own_code_revision: str | None, stored_fingerprint: dict | None
) -> str | None:
    """P3 gate review item 4: a run created by a different deployment is left
    `queued` untouched rather than claimed and failed at fingerprint
    verification (ThreadExecutor's own admission-time check,
    orchestrator/executor.py -- the SAME code_revision comparison, so a run
    this note names is exactly one that executor would also skip). Surfaced
    here, read-only, so the UI can show WHY a queued run is not progressing.
    Returns None whenever there is nothing to say -- not queued, this
    deployment's own code_revision is not configured (dev/local, where every
    run is presumed this deployment's own), or the fingerprint row cannot be
    read (`stored_fingerprint` is None). Takes an already-resolved
    fingerprint dict rather than fetching one itself (independent review
    2026-09-24 item 6) -- callers listing many runs fetch every needed
    fingerprint in one batched call (get_fingerprints) rather than one
    query per run; the single-run caller (get_run, below) still fetches
    its own."""
    if status != "queued":
        return None
    if own_code_revision is None:
        return None
    stored_code_revision = (stored_fingerprint or {}).get("code_revision")
    if stored_code_revision and stored_code_revision != own_code_revision:
        return f"queued — created by a different deployment (code revision {stored_code_revision[:12]})"
    return None


def _queue_affinity_note(ctx: AppContext, status: str, fingerprint_id: str | None) -> str | None:
    """Single-fetch wrapper around _queue_affinity_note_from_fingerprint for
    a caller with exactly one run to check (get_run) -- list_runs fetches
    every fingerprint it needs in one batched call instead of calling this."""
    if status != "queued" or not fingerprint_id:
        return None
    own_code_revision = getattr(ctx.settings, "code_revision", None)
    if own_code_revision is None:
        return None
    try:
        stored_fingerprint = ctx.persistence.get_fingerprint(fingerprint_id)
    except Exception:
        return None
    return _queue_affinity_note_from_fingerprint(status, own_code_revision, stored_fingerprint)


def get_run(ctx: AppContext, run_id: str, *, state: RunState | None = None) -> dict:
    # `state`, when given, is a RunState the caller already loaded for this
    # same run_id (never for a different run -- that is the caller's bug, not
    # this function's to detect) -- skips a second persistence.load_state
    # round trip. app/src/platform/adapters.get_run_and_narration is the one
    # caller that passes it, to avoid the exact duplicate load /run/<id>'s
    # poll callback used to make on every render (P3/P4 perf gap review
    # 2026-09-25).
    if state is None:
        state = ctx.persistence.load_state(run_id)
    payload = json.loads(to_json(state))
    payload["status_label"] = state.status.replace("_", " ").title()
    payload["queue_note"] = _queue_affinity_note(ctx, state.status, state.fingerprint_id)

    # CLAUDE.md §11 "Paused runs across a code deploy" / independent review
    # 2026-09-24 gap #11: only computed once a run is completed (never on
    # every poll tick of a still-active run -- run_status.py stops polling
    # at "completed" anyway) so a run whose export ran under a different
    # code revision than it was created under can say so, without adding
    # query cost to the common, unpaused case.
    if state.status == "completed":
        fingerprint = ctx.persistence.get_fingerprint(state.fingerprint_id)
        payload["computed_code_revision"] = fingerprint.get("code_revision")
        run_row = ctx.persistence.get_run_row(run_id)
        payload["export_code_revision"] = (run_row or {}).get("export_code_revision")

    nodes = NODES_FOR.get(state.run_kind, {}).get(state.phase, [])
    current_stage = None
    if 0 <= state.next_node_index < len(nodes):
        current_stage = NODE_STAGE_LABELS.get(nodes[state.next_node_index][0])
    payload["progress"] = {
        "node_index": state.next_node_index,
        "total_nodes": len(nodes),
        "current_stage": current_stage,
    }
    return payload


def list_runs(
    ctx: AppContext, filters: dict | None = None, *,
    runs: list[dict] | None = None, versions: list[dict] | None = None,
) -> list[dict]:
    """Independent review 2026-09-24 item 6: this used to issue 3 extra
    queries PER RUN (list_findings, list_management_actions, get_run_metrics)
    plus a 4th for any queued run (get_fingerprint) -- a genuine N+1 query
    cost, and directly the shape of query the 2026-09-23 idle-cost incident
    was about. Every one of those is now a single batched call for the
    WHOLE page, keyed by run_id, looked up per row from an in-memory dict --
    same output, same per-run logic, one round trip per data source instead
    of one per run.

    `runs`, when given, is an already-fetched, UNFILTERED
    `ctx.persistence.list_runs()` result (get_actions_page_data below is the
    one real caller) -- skips this function's own `ctx.persistence.
    list_runs()` round trip. Only honoured when `filters` is falsy: a
    filtered caller must always get its own filtered read.

    P3/P4 perf gap review 2026-09-25, live pass: the 5 reads below --
    list_skills' own skill-directory/list_all_skill_versions work, and the
    4 batched-by-run-id reads -- are each independent of the others (none
    consumes another's result; they all key off `rows`/`run_ids`, computed
    once above). Measured live against the real warehouse: ~0.5-1.3s each,
    run sequentially that was 5 round trips no caller needed to wait on one
    at a time. Same fan-out pattern as app/src/platform/adapters.py's
    `_read_narration_sources` (bounded by the persistence layer's own
    connection pool; LocalPersistence stays sequential for the same
    single-shared-sqlite-connection reason that function's own docstring
    gives)."""
    rows = ctx.persistence.list_runs(filters=filters) if runs is None else runs
    data_mode = "Local test data" if ctx.backend == "local" else "Unity Catalog"

    run_ids = [r["run_id"] for r in rows]
    own_code_revision = getattr(ctx.settings, "code_revision", None)
    queued_fingerprint_ids = [
        r["fingerprint_id"] for r in rows if r["status"] == "queued" and r.get("fingerprint_id")
    ]

    # list_skills' own runs-by-skill grouping needs the COMPLETE run set to
    # produce a correct "previous_runs"/"last_run" per skill -- `rows` only
    # qualifies as that when this call itself was unfiltered; a filtered
    # call (e.g. by skill_id or status) leaves `runs=None` so list_skills
    # does its own single unfiltered fetch instead of grouping a narrowed set.
    reads = (
        lambda: list_skills(
            ctx, runs=rows if not filters else None, versions=versions if not filters else None,
        ),
        lambda: ctx.persistence.list_findings_for_runs(run_ids),
        lambda: ctx.persistence.list_management_actions_for_runs(run_ids),
        lambda: ctx.persistence.get_run_metrics_for_runs(run_ids),
        lambda: ctx.persistence.get_fingerprints(queued_fingerprint_ids),
    )
    from orchestrator.adapters.persistence_local import LocalPersistence

    if isinstance(ctx.persistence, LocalPersistence):
        skills, findings_by_run, actions_by_run, metrics_by_run, fingerprints_by_id = (fn() for fn in reads)
    else:
        from concurrent.futures import ThreadPoolExecutor

        max_workers = min(len(reads), max(1, getattr(ctx.settings, "max_connections", len(reads))))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(fn) for fn in reads]
            skills, findings_by_run, actions_by_run, metrics_by_run, fingerprints_by_id = (
                f.result() for f in futures
            )
    skills_by_id = {e["skill_id"]: e for e in skills}

    out = []
    for r in rows:
        run_id = r["run_id"]
        findings = findings_by_run.get(run_id, [])
        actions = actions_by_run.get(run_id, [])
        open_actions = [a for a in actions if a.get("status") != "closed"]
        exposure_metric = metrics_by_run.get(run_id, {}).get("run_exposure_headline")
        skill_entry = skills_by_id.get(r.get("skill_id"))

        approved_by = r.get("approved_by")
        legacy_signoff_policy = evaluate_signoff(actor=approved_by, run_owner=r["run_owner"])
        # P7 review workflow (§3.7): label_for derives self_approved from
        # runs.prepared_by/reviewed_by/approved_by (all already-projected
        # columns, no per-run RunState read for a whole page of rows) --
        # falling back to the pre-P7 approved_by==run_owner rule when
        # prepared_by is null (a run the P7 workflow never touched). Either
        # way this is the SAME single policy decision
        # (orchestrator/signoff_policy.py), never re-derived ad hoc here.
        self_approved = bool(
            label_for(
                {
                    "prepared_by": r.get("prepared_by"),
                    "reviewed_by": r.get("reviewed_by"),
                    "approved_by": approved_by,
                    "self_approved": bool(approved_by) and legacy_signoff_policy["self_approved"],
                }
            )
        )

        out.append(
            {
                "run_id": run_id,
                "skill_id": r.get("skill_id"),
                "engagement_id": r.get("engagement_id"),
                "skill_name": (
                    skill_entry["name"] if skill_entry
                    # docs/specs/P6_P8_explorer_llm_design.md §5.2: an
                    # Explorer run's skill_id is None until, at the
                    # earliest, plan confirmation (§4.1's RunState mapping
                    # -- state.skill_id never carries the EXPLORER-<run_id>
                    # ledger id, only resolve_run_skill does, at execute
                    # time), so it has no skill card to fall back to.
                    else f"Explorer: {r['objective'][:60]}" if r.get("mode") == "explorer"
                    else r.get("skill_id")
                ),
                "audit_period": f"{r['audit_period_start']} – {r['audit_period_end']}",
                # Independent review 2026-09-24 gap #6: cross_run_totals reads
                # this back (CLAUDE.md §11 "Paused runs across a code
                # deploy") to exclude a run explicitly marked superseded.
                "superseded_by": r.get("superseded_by"),
                "run_timestamp": r["created_at"],
                "run_owner": r["run_owner"],
                "data_mode": data_mode,
                "status": str(r["status"]).replace("_", " ").title(),
                "findings_count": len(findings),
                "high_risk_count": sum(1 for f in findings if f["severity"] == "High"),
                # B4 (CLAUDE.md NN14): a run that has not yet reached
                # `prioritise` (or one whose whole finding set is non-monetary)
                # has no run_exposure_headline metric -- None, never a
                # fabricated 0 that would misrepresent "nothing at risk".
                "potential_exposure": exposure_metric["value"] if exposure_metric else None,
                "open_actions": len(open_actions),
                "last_updated": r["last_state_change_at"],
                "has_workspace": bool(skill_entry and skill_entry.get("has_workspace")),
                "approved_by": approved_by,
                "self_approved": self_approved,
                "sod_enforced": legacy_signoff_policy["sod_enforced"],
                "queue_note": _queue_affinity_note_from_fingerprint(
                    r["status"], own_code_revision, fingerprints_by_id.get(r.get("fingerprint_id"))
                ),
            }
        )
    return out


# Independent review 2026-09-24 gap #6: the statuses a run must reach before
# it counts towards a cross-run total. "Completed" and "Awaiting Signoff"
# both carry a trustworthy, finished test result (a run in "Awaiting
# Signoff" has already run every test and computed every number -- only
# export is outstanding); "Queued"/"Running"/"Awaiting Confirmation" have
# not, and "Failed"/"Interrupted" never will for this attempt.
_ELIGIBLE_STATUSES_FOR_TOTALS = {"Completed", "Awaiting Signoff"}


def _parse_audit_period(audit_period: str | None) -> tuple[str | None, str | None]:
    # list_runs()'s own "{start} – {end}" formatting (CLAUDE.md build brief
    # P3 §4) is the one shape every caller of cross_run_totals already
    # carries -- parsed back here rather than requiring a second,
    # separate-fields copy of the same two dates on every run dict.
    if not audit_period or " – " not in audit_period:
        return None, None
    start, end = audit_period.split(" – ", 1)
    return start.strip(), end.strip()


def _periods_overlap(a: tuple[str | None, str | None], b: tuple[str | None, str | None]) -> bool:
    a_start, a_end = a
    b_start, b_end = b
    # A period with a missing or unparseable bound can't be proven disjoint
    # from another -- conservatively treat it as overlapping rather than
    # risk a silent double-count (CLAUDE.md NN14).
    if not a_start or not a_end or not b_start or not b_end:
        return True
    return a_start <= b_end and b_start <= a_end


def cross_run_totals(runs: list[dict]) -> dict:
    """Independent review 2026-09-24 gap #6: one rule, computed once, for
    both cross-run KPIs that used to double-count -- `/runs`' "High-risk
    findings" (summed every run, including re-runs and failed ones) and
    `/actions`' "Total exposure" (already deduplicated exact re-runs of the
    same skill/engagement/period -- independent review item 2 -- but still
    summed runs whose periods merely OVERLAP without being identical, the
    same spend counted under two windows).

    Takes the list `list_runs()` already returned (never re-queries):
      1. eligible = status in _ELIGIBLE_STATUSES_FOR_TOTALS and not
         superseded (`superseded_by` falsy) -- CLAUDE.md §11 "Paused runs
         across a code deploy".
      2. Where the same (skill_id, engagement_id, audit_period) was run more
         than once, only the latest (by run_timestamp) survives -- a re-run
         is the same population re-tested, not additional risk.
      3. high_risk_findings_total sums high_risk_count over that
         deduplicated set. The "never sum across overlapping periods" rule
         below is specifically about AMOUNTS AT RISK, not finding counts.
      4. total_exposure: if no two surviving runs have overlapping audit
         periods, it is the sum of potential_exposure over the set (a run
         with no exposure metric contributes nothing, never a fabricated
         $0). If any two DO overlap, summing would double-count the same
         spend under two windows -- report the single latest run's own
         figure instead. None (rendered "--", never a fabricated $0) when
         there is nothing eligible to report."""
    eligible = [
        r for r in runs
        if r.get("status") in _ELIGIBLE_STATUSES_FOR_TOTALS and not r.get("superseded_by")
    ]
    latest_by_group: dict[tuple, dict] = {}
    for r in eligible:
        key = (r.get("skill_id"), r.get("engagement_id"), r.get("audit_period"))
        current = latest_by_group.get(key)
        if current is None or (r.get("run_timestamp") or "") > (current.get("run_timestamp") or ""):
            latest_by_group[key] = r
    candidates = list(latest_by_group.values())

    high_risk_findings_total = sum(r.get("high_risk_count", 0) or 0 for r in candidates)

    if not candidates:
        return {"high_risk_findings_total": high_risk_findings_total, "total_exposure": None}

    periods = [_parse_audit_period(r.get("audit_period")) for r in candidates]
    overlaps = any(
        _periods_overlap(periods[i], periods[j])
        for i in range(len(periods)) for j in range(i + 1, len(periods))
    )
    if overlaps:
        latest = max(candidates, key=lambda r: r.get("run_timestamp") or "")
        total_exposure = latest.get("potential_exposure")
    else:
        exposures = [r.get("potential_exposure") for r in candidates if r.get("potential_exposure") is not None]
        total_exposure = sum(exposures) if exposures else None

    return {"high_risk_findings_total": high_risk_findings_total, "total_exposure": total_exposure}


def list_trace_events(ctx: AppContext, run_id: str | None = None) -> list[dict]:
    return ctx.persistence.list_trace_events(run_id)


def list_management_actions(
    ctx: AppContext, filters: dict | None = None, *,
    runs: list[dict] | None = None, versions: list[dict] | None = None,
) -> list[dict]:
    """`runs`/`versions`, when given, are already-fetched, UNFILTERED
    `ctx.persistence.list_runs()`/`list_all_skill_versions()` results,
    threaded straight into `list_skills` -- see `get_actions_page_data`
    below, the one caller that needs to share them with a second,
    independent `list_runs()` read rather than each issuing its own."""
    rows = ctx.persistence.list_management_actions(filters=filters)
    skills_by_id = {e["skill_id"]: e for e in list_skills(ctx, runs=runs, versions=versions)}
    out = []
    for r in rows:
        skill_entry = skills_by_id.get(r.get("skill_id"))
        out.append(
            {
                "action_id": r["action_id"],
                "finding_title": r.get("finding_title") or r.get("title"),
                "skill_id": r.get("skill_id"),
                "skill_name": skill_entry["name"] if skill_entry else r.get("skill_id"),
                "run_id": r.get("run_id"),
                "risk": r.get("risk"),
                "owner": r.get("owner"),
                "status": str(r.get("status") or "").replace("_", " ").title(),
                "target_date": r.get("target_date"),
                "potential_exposure": r.get("potential_exposure"),
                "last_updated": r.get("last_updated"),
                "evidence_link": r.get("evidence_link"),
                # Independent review 2026-09-24 gap #3: workspace_tne.py's
                # _action_row_view reads `description` back as the "response"
                # column (the same field an edit's `response` overwrites via
                # update_management_action below) -- omitted here, a
                # persisted edit could never be read back on reload.
                "description": r.get("description"),
                "updated_by": r.get("updated_by"),
            }
        )
    return out


def get_actions_page_data(ctx: AppContext, filters: dict | None = None) -> tuple[list[dict], list[dict]]:
    """`management_actions_page()` (app/src/platform/pages.py) needs BOTH
    `list_management_actions()`'s UI rows (the action table, filtered by
    `filters`) and `list_runs()`'s UI rows (for `cross_run_totals`' Total-
    exposure KPI, always unfiltered) -- each used to independently issue its
    own `ctx.persistence.list_runs()` AND `list_all_skill_versions()` call
    via its own internal `list_skills(ctx)` lookup (BUG-ACTIONS-3, P3/P4
    perf gap review 2026-09-25 live pass: two full round trips fired TWICE
    each on every /actions render -- skill_versions alone measured 0.9-3.2s
    per call against the real warehouse). Fetched here ONCE and threaded
    into both."""
    raw_runs = ctx.persistence.list_runs()
    raw_versions = ctx.persistence.list_all_skill_versions()
    actions = list_management_actions(ctx, filters=filters, runs=raw_runs, versions=raw_versions)
    runs = list_runs(ctx, runs=raw_runs, versions=raw_versions)
    return actions, runs


# Independent review 2026-09-24 gap #3: the Management Action Tracker's own
# status dropdown values (app/src/workspace_tne.py _actions_tab), the same
# set management_actions_status's CHECK constraint enforces
# (orchestrator/ddl/*/012_management_action_edits.sql).
_MANAGEMENT_ACTION_STATUSES = {"draft", "open", "under_review", "in_progress", "agreed", "remediated", "closed"}


def update_management_action(
    ctx: AppContext, action_id: str, *, owner: str | None, status: str, target_date: str | None,
    response: str | None, actor: str,
) -> dict:
    """Independent review 2026-09-24 gap #3: persists an auditor's edit to a
    management action's owner/status/target_date/response, replacing the
    browser-session-only dcc.Store the Management Action Tracker used to
    hold edits in -- CLAUDE.md §11's "Action ownership and responses are
    saved with this run" is only true once edits write through to Delta /
    LocalPersistence, recording who made the edit (`updated_by`) alongside
    the existing `last_updated` "when". `status` is validated against the
    Tracker's own set before it ever reaches the persistence layer, rather
    than surfacing a raw constraint-violation error for an unreachable
    caller mistake."""
    if status not in _MANAGEMENT_ACTION_STATUSES:
        raise ValueError(f"unknown management action status: {status!r}")
    now = ctx.clock()
    row = ctx.persistence.update_management_action(
        action_id, owner=owner, status=status, target_date=target_date, response=response,
        updated_by=actor, now=now,
    )
    return {
        "action_id": row["action_id"],
        "owner": row.get("owner"),
        "status": str(row.get("status") or "").replace("_", " ").title(),
        "target_date": row.get("target_date"),
        "response": row.get("description"),
        "updated_by": row.get("updated_by"),
        "last_updated": row.get("last_updated"),
    }


def _confirm_explorer_plan(ctx: AppContext, run_id: str, actor: str) -> RunState:
    """docs/specs/P6_P8_explorer_llm_design.md §4.10 "confirm_plan (Explorer
    branch)": narrow the proposal by the accumulated plan_edits, revalidate,
    require at least one included test still valid, run the V-Z1
    belt-and-braces check (§4.7), materialise + record the EXPLORER-<run_id>
    ledger row, register its proposed risks/controls, then set
    plan_confirmed/confirmed_plan_hash and transition exactly like the
    Playbook path (orchestrator.runs.confirm_plan) does."""
    now = ctx.clock()
    state = ctx.persistence.load_state(run_id)
    if state.status != "awaiting_confirmation" or state.mode != "explorer":
        raise ExplorerPlanNotConfirmable(
            f"run {run_id!r} is not an Explorer run awaiting_confirmation "
            f"(status={state.status!r}, mode={state.mode!r})"
        )
    plan = state.plan or {}
    proposal = plan.get("proposal")
    if plan.get("status") != "proposed" or proposal is None:
        raise ExplorerPlanNotConfirmable(
            f"run {run_id!r}: plan.status is {plan.get('status')!r}, not 'proposed' -- "
            f"nothing to confirm"
        )

    effective = apply_plan_edits(proposal, state.plan_edits)
    sources = (state.profile_result or {}).get("sources", {})
    data_source = _build_explorer_data_source(ctx, state)
    pinned_versions = {b["source"]: b["version"] for b in state.data_assets}
    try:
        report = validate_proposal(
            effective, profile=sources, run_sources=sorted(sources), data_source=data_source,
            pinned_versions=pinned_versions,
        )
    finally:
        _close_data_source(data_source)
    valid_tests = [t for t in effective.get("tests", []) if report["tests"].get(t["key"], {}).get("valid")]
    if not valid_tests:
        raise ExplorerPlanNotConfirmable(f"run {run_id!r}: no included test is valid -- nothing to confirm")
    valid_keys = {t["key"] for t in valid_tests}
    effective = dict(effective)
    effective["tests"] = valid_tests
    effective["findings"] = [f for f in effective.get("findings", []) if f.get("test_key") in valid_keys]

    options = (state.options or {}).get("explorer", {})
    run_sources_for_materialise = _explorer_run_sources_for_materialise(options)
    audit_timezone = options.get("audit_timezone") or getattr(ctx.settings, "audit_timezone", None)

    # V-Z1 (§4.7): should never fire against an already-validated subset,
    # but this is the belt-and-braces check that catches a violation the
    # named rules could not -- refused before anything is written, never
    # silently materialised.
    vz1 = check_materialised_skill(
        effective, profile=sources, run_sources=run_sources_for_materialise, run_id=run_id,
        confirmed_at=now, owner=actor, audit_timezone=audit_timezone,
    )
    if vz1["proposal_errors"] or vz1["test_violations"]:
        raise ExplorerPlanNotConfirmable(f"run {run_id!r}: materialised Skill failed validation (V-Z1): {vz1}")

    files = materialise(
        effective, profile=sources, run_sources=run_sources_for_materialise, run_id=run_id,
        confirmed_at=now, owner=actor, audit_timezone=audit_timezone,
    )
    content_hash = hash_skill_content_entries(list(files.items()))
    content = {
        "files": {name: text.decode("utf-8") for name, text in files.items()},
        "origin": "explorer_run", "source_run_id": run_id,
    }
    ctx.persistence.record_skill_version(
        skill_id=f"EXPLORER-{run_id}", version="run", content_hash=content_hash, content=content,
        created_by=actor, now=now, status="draft",
    )

    rc = yaml.safe_load(files["risk_control.yaml"].decode("utf-8")) or {"risks": [], "controls": []}
    risks = [
        {"risk_id": r["risk_id"], "title": r["title"], "description": r.get("description"),
         "category": None, "status": "proposed", "source": "explorer"}
        for r in rc.get("risks", [])
    ]
    controls = [
        {"control_id": c["control_id"], "risk_id": c["risk_id"], "title": c["title"],
         "description": c.get("description"), "type": c.get("type"), "frequency": None}
        for c in rc.get("controls", [])
    ]
    if risks:
        ctx.persistence.upsert_risks(risks, now=now)
    if controls:
        ctx.persistence.upsert_controls(controls, now=now)

    state = replace(state, plan_confirmed=True, confirmed_plan_hash=content_hash)
    new_state = transition(state, "queued", now=now, phase="execute")
    saved = ctx.persistence.save_state(new_state)
    message = f"Explorer plan confirmed by {actor} — {len(valid_tests)} test(s), content {content_hash[:12]}"
    runs_module._emit(ctx.persistence, saved, event_type="plan_confirmed", actor=actor, message=message, now=now)
    return saved


def _refuse_if_stale_before_execute(ctx: AppContext, state: RunState) -> None:
    """CLAUDE.md §11 "Paused runs across a code deploy" / independent review
    2026-09-24 gap #11: confirming a plan is the point this run's execute
    phase is about to run for the first time under WHATEVER setup is current
    right now -- unlike sign-off, execute has not yet fixed this run's
    numbers, so it must never silently run under a setup the run was not
    created for. Raises RunCodeRevisionStale (never a bare FingerprintMismatch)
    so the caller can show a message that names the resolution: restart the
    run with restart_stale_run."""
    stored_fingerprint = ctx.persistence.get_fingerprint(state.fingerprint_id)
    current_fingerprint = build_run_fingerprint(ctx, state)
    try:
        verify_fingerprint(stored_fingerprint, current_fingerprint)
    except FingerprintMismatch as exc:
        raise RunCodeRevisionStale(state.run_id, state.phase, exc.differing_fields) from exc


def confirm_plan(ctx: AppContext, run_id: str, actor: str) -> RunState:
    current = ctx.persistence.load_state(run_id)
    _refuse_if_stale_before_execute(ctx, current)
    if current.mode == "explorer":
        state = _confirm_explorer_plan(ctx, run_id, actor)
    else:
        state = runs_module.confirm_plan(ctx.persistence, run_id, actor=actor, now=ctx.clock())
    if ctx.executor is not None:
        ctx.executor.start(run_id, state.phase)
    return state


def restart_stale_run(ctx: AppContext, run_id: str, actor: str) -> str:
    """CLAUDE.md §11 "Paused runs across a code deploy" / independent review
    2026-09-24 gap #11: the "one click" restart for a run that
    _refuse_if_stale_before_execute (or an interrupted-run resume,
    orchestrator.runs.resume) refused -- a run paused before its execute
    phase completed, on a code revision this deployment no longer matches.
    Starts a fresh run with exactly the old run's own parameters (skill,
    bindings, audit period, objective, mode, owner and options), re-resolving
    source versions fresh (the same TOCTOU-safe path start_audit_run always
    takes -- never the old run's pinned versions, which may themselves be
    stale), then marks the old run `superseded_by` the new one -- never
    deleted (CLAUDE.md §9A Q2). Does not itself check staleness or the old
    run's status/phase: a caller decides when a restart is warranted."""
    old_state = ctx.persistence.load_state(run_id)
    bindings = {b["source"]: b["table_fqn"] for b in old_state.data_assets}
    options = old_state.options or {}
    new_run_id = start_audit_run(
        ctx,
        skill_id=old_state.skill_id,
        bindings=bindings,
        audit_period=old_state.audit_period,
        objective=old_state.objective,
        run_owner=old_state.run_owner,
        mode=old_state.mode,
        review_plan_first=not options.get("auto_confirm_plan", True),
        engagement_id=old_state.engagement_id,
        business_unit=old_state.business_unit,
        materiality=old_state.materiality,
        generate_management_actions=options.get("generate_management_actions", True),
        jira_preview_requested=options.get("jira_preview_requested", False),
    )
    now = ctx.clock()
    ctx.persistence.mark_run_superseded(run_id, superseded_by=new_run_id, now=now)
    _emit_service_event(
        ctx.persistence, old_state,
        event_id=_service_trace_event_id(run_id, "superseded", new_run_id),
        event_type="run_superseded",
        actor=actor,
        message=f"Superseded by {new_run_id} (code revision changed before this run's execute phase completed)",
        now=now,
    )
    return new_run_id


def sign_off(ctx: AppContext, run_id: str, actor: str) -> RunState:
    # role_resolver/settings are always passed through -- orchestrator.runs.
    # sign_off only ever touches them on the P7-gated path (state.review.stage
    # == 'approval'), which itself required prepare()/mark_reviewed() to have
    # already run, so a legacy self-sign-off (state.review is None) is
    # completely unaffected (LazyRoleResolver defers the ConfigError an
    # unconfigured REVIEW_* environment would otherwise raise eagerly).
    state = runs_module.sign_off(
        ctx.persistence, run_id, actor=actor, now=ctx.clock(),
        role_resolver=_role_resolver_for(ctx), settings=ctx.settings,
    )
    if ctx.executor is not None:
        ctx.executor.start(run_id, state.phase)
    return state


# ── P6 WP N9: AI-proposed finding decisions, narrative edits and narration
# regenerate (docs/specs/P6_narration_design.md §5.2, §5.5, §6.4) ───────────


def _service_trace_event_id(run_id: str, *parts: str) -> str:
    """Unlike `runs_module._emit` (keyed on `run_id:event_type:state_version`,
    correct for the ONE transition-triggered event a RunState change ever
    produces), several `decide_candidate`/`edit_narrative` calls can legally
    happen back-to-back with `state_version` unchanged -- neither call
    touches RunState. Keying on the decided/edited row's own id instead
    (never state_version) keeps every one of those a distinct trace_events
    row instead of the second silently no-op'ing into `append_trace_event`'s
    INSERT OR IGNORE on a colliding event_id."""
    return hashlib.sha256(":".join((run_id, *parts)).encode("utf-8")).hexdigest()[:32]


def _emit_service_event(persistence, state: RunState, *, event_id: str, event_type: str, actor: str, message: str, now: str) -> None:
    persistence.append_trace_event(
        {
            "event_id": event_id,
            "run_id": state.run_id,
            "engagement_id": state.engagement_id,
            "event_type": event_type,
            "event_time": now,
            "stage": state.phase,
            "status": state.status,
            "message": message,
            "duration_s": None,
            "node_name": None,
            "execution_key": None,
            "actor": actor,
            "state_version": state.state_version,
        }
    )


# ── P7 review workflow wrappers (docs/specs/P7_mapping_authoring_design.md
# §3.3, D-P7-10) ──────────────────────────────────────────────────────────


def _refuse_unless_preparation_stage(ctx: AppContext, state: RunState, action: str, actor: str) -> None:
    """D-P7-10: only the preparer edits model text and decides AI-proposed
    findings, and only during `preparation` -- a legacy run whose P7 workflow
    never ran (`state.review is None`) is unrestricted, exactly as before."""
    review = state.review or {}
    if not review or review.get("stage") == "preparation":
        return
    now = ctx.clock()
    message = "Text can only be edited during preparation — ask the reviewer to return the run."
    _emit_service_event(
        ctx.persistence, state,
        event_id=_service_trace_event_id(state.run_id, action, "refused", now),
        event_type="review_action_refused", actor=actor,
        message=f"{action} refused: {message}", now=now,
    )
    raise ReviewActionRefused(state.run_id, action, message)


def _role_resolver_for(ctx: AppContext):
    return LazyRoleResolver(ctx.settings)


def prepare_findings(ctx: AppContext, run_id: str, actor: str) -> RunState:
    state = runs_module.prepare(
        ctx.persistence, run_id, actor=actor, now=ctx.clock(),
        role_resolver=_role_resolver_for(ctx), settings=ctx.settings,
    )
    return state


def mark_reviewed(ctx: AppContext, run_id: str, actor: str) -> RunState:
    return runs_module.mark_reviewed(
        ctx.persistence, run_id, actor=actor, now=ctx.clock(),
        role_resolver=_role_resolver_for(ctx), settings=ctx.settings,
    )


def return_to_preparer(ctx: AppContext, run_id: str, actor: str, reason: str) -> RunState:
    return runs_module.return_to_preparer(
        ctx.persistence, run_id, actor=actor, reason=reason, now=ctx.clock(),
        role_resolver=_role_resolver_for(ctx), settings=ctx.settings,
    )


def raise_review_note(ctx: AppContext, run_id: str, actor: str, body: str, finding_id: str | None = None) -> dict:
    return runs_module.raise_review_note(
        ctx.persistence, run_id, actor=actor, body=body, finding_id=finding_id, now=ctx.clock(),
        role_resolver=_role_resolver_for(ctx), settings=ctx.settings,
    )


def respond_to_review_note(ctx: AppContext, run_id: str, note_id: str, actor: str, response: str) -> dict:
    return runs_module.respond_to_review_note(
        ctx.persistence, run_id, note_id, actor=actor, response=response, now=ctx.clock(),
        role_resolver=_role_resolver_for(ctx), settings=ctx.settings,
    )


def clear_review_note(ctx: AppContext, run_id: str, note_id: str, actor: str) -> dict:
    return runs_module.clear_a_review_note(
        ctx.persistence, run_id, note_id, actor=actor, now=ctx.clock(),
        role_resolver=_role_resolver_for(ctx), settings=ctx.settings,
    )


_CANDIDATE_DECISIONS = ("accepted", "rejected")


def decide_candidate(
    ctx: AppContext,
    run_id: str,
    candidate_id: str,
    *,
    decision: str,
    reason: str | None = None,
    decided_severity: str | None = None,
    actor: str,
) -> dict:
    """§5.2: `decide_candidate(ctx, run_id, candidate_id, *, decision, reason,
    actor)`, with the auditor's own `decided_severity` (§14 Answers Q4 -- the
    model's `proposed_severity` is shown, never applied silently; both are
    stored). The single-winner guarantee is `decide_candidate_cas`'s own
    conditional UPDATE (WHERE candidate_status='candidate') -- this function
    turns a lost race into the right exception rather than a silent no-op:
    `CandidateAlreadyDecided` (another decide_candidate won first) or
    `CandidateSuperseded` (a racing `regenerate_narration` won first, T-C6)."""
    if decision not in _CANDIDATE_DECISIONS:
        raise ValueError(f"decision must be one of {_CANDIDATE_DECISIONS}, got {decision!r}")
    state = ctx.persistence.load_state(run_id)
    if state.status != "awaiting_signoff":
        raise RunNotAwaitingSignoff(run_id, state.status)
    _refuse_unless_preparation_stage(ctx, state, "candidate_decided", actor)
    if decision == "accepted" and not decided_severity:
        raise CandidateSeverityRequired(candidate_id)
    if decision == "rejected" and not (reason and reason.strip()):
        raise CandidateReasonRequired(candidate_id)

    now = ctx.clock()
    won = ctx.persistence.decide_candidate_cas(
        candidate_id,
        decision=decision,
        reason=reason,
        decided_severity=decided_severity if decision == "accepted" else None,
        actor=actor,
        now=now,
    )
    current = next(
        (c for c in ctx.persistence.list_candidates(run_id) if c["candidate_id"] == candidate_id), None
    )
    if current is None:
        raise CandidateNotFound(candidate_id)
    if not won:
        if current["candidate_status"] == "superseded":
            raise CandidateSuperseded(candidate_id)
        raise CandidateAlreadyDecided(candidate_id, current["candidate_status"])

    message = f"{current['rule_id']}: {decision} by {actor}"
    if reason:
        message += f" — {reason}"
    event_type = "candidate_accepted" if decision == "accepted" else "candidate_rejected"
    _emit_service_event(
        ctx.persistence, state,
        event_id=_service_trace_event_id(run_id, candidate_id, event_type),
        event_type=event_type, actor=actor, message=message, now=now,
    )
    return current


# `narratives.field` (§6.1: "observation|recommendation|management_questions|
# title|summary|root_cause|review_observations|rationale|remediation|
# exec_summary|caption|profile") -> the validator's own field taxonomy
# (`orchestrator.narration.lexicon.FIELD_LENGTH_CAPS`/`OBSERVATION_TYPE_
# FIELDS`/`TITLE_FIELDS`/`RATIONALE_FIELD`) -- the SAME mapping
# `orchestrator.narration.runner`'s per-task `narrate_*` functions apply when
# they first validate the model's own output (e.g. `narrate_remediation`
# reuses the "recommendation" field's rules, its own comment says so), kept
# here rather than imported so this module never has to import runner.py's
# node-scoped internals for one small lookup table.
_VALIDATOR_FIELD_FOR: dict[tuple[str, str], str] = {
    ("finding", "observation"): "observation",
    ("finding", "recommendation"): "recommendation",
    ("finding", "management_questions"): "question",
    ("finding", "rationale"): "rationale",
    ("finding", "remediation"): "recommendation",
    ("candidate", "observation"): "observation",
    ("candidate", "recommendation"): "recommendation",
    ("candidate", "management_questions"): "question",
    ("candidate", "title"): "candidate_title",
    ("candidate", "rationale"): "rationale",
    ("theme", "title"): "theme_title",
    ("theme", "summary"): "theme_summary",
    ("theme", "root_cause"): "root_cause",
    ("theme", "review_observations"): "review_observation",
    ("run", "exec_summary"): "exec_paragraph",
    ("chart", "caption"): "caption",
    ("profile", "profile"): "profile_paragraph",
}
# §3.2's list-shaped fields -- `narratives.template_text` for these holds a
# JSON array (`orchestrator.narration.runner._persist`'s `list_text` path),
# so an edit's `new_text` may be either a single replacement string (treated
# as the whole list becoming that one item) or the full list.
_LIST_NARRATIVE_FIELDS = frozenset({"management_questions", "review_observations", "exec_summary", "profile"})


def _metrics_placeholder_table(names, metrics: dict[str, dict]):
    from orchestrator.narration.placeholders import PlaceholderEntry, class_for_unit

    table = {}
    for name in names:
        row = metrics.get(name)
        if row is None or row.get("value") is None:
            continue
        unit = row.get("unit")
        table[name] = PlaceholderEntry(
            name=name, unit=unit, value=row["value"], source_field=f"run_metrics.{name}",
            meaning=f"{class_for_unit(unit)} metric, unit {unit}",
        )
    return table


def _narrative_table(ctx: AppContext, state: RunState, row: dict) -> dict:
    """Rebuilds the SAME `{name: PlaceholderEntry}` table
    `orchestrator.narration.runner` built when this narrative was first
    generated (§3.2) -- from this run's CURRENT persisted findings/
    candidates/themes/metrics, never re-run through a model. This is what
    `validate_human_edit`'s N-H1 check (a typed number must equal the
    rendering of a metric the item cites) validates an edit against."""
    from orchestrator.catalogue_counts import catalogue_tests_for_skill
    from orchestrator.narration.payloads import build_finding_table, build_profile_payload, build_theme_table

    target_kind = row["target_kind"]
    target_id = row["target_id"]
    period = tuple(state.audit_period) if state.audit_period else None

    if target_kind == "finding":
        finding = next(
            (f for f in ctx.persistence.list_findings(state.run_id) if f["finding_id"] == target_id), None
        )
        if finding is None:
            raise NarrativeTargetNotFound(row["narrative_id"], target_kind, target_id)
        skill = resolve_run_skill(ctx, state)
        return build_finding_table(finding, skill=skill, period=period)
    if target_kind == "candidate":
        candidate = next(
            (c for c in ctx.persistence.list_candidates(state.run_id) if c["candidate_id"] == target_id), None
        )
        if candidate is None:
            raise NarrativeTargetNotFound(row["narrative_id"], target_kind, target_id)
        metrics = ctx.persistence.get_run_metrics(state.run_id)
        return _metrics_placeholder_table(candidate.get("metrics_cited") or [], metrics)
    if target_kind == "theme":
        theme = next(
            (t for t in ctx.persistence.list_themes(state.run_id) if t["theme_id"] == target_id), None
        )
        if theme is None:
            raise NarrativeTargetNotFound(row["narrative_id"], target_kind, target_id)
        findings_by_id = {f["finding_id"]: f for f in ctx.persistence.list_findings(state.run_id)}
        members = [findings_by_id[fid] for fid in theme.get("finding_ids", []) if fid in findings_by_id]
        skill = resolve_run_skill(ctx, state)
        return build_theme_table(members, skill=skill)
    if target_kind == "run":
        from orchestrator.narration.payloads import build_run_table

        findings = ctx.persistence.list_findings(state.run_id)
        metrics = ctx.persistence.get_run_metrics(state.run_id)
        skill = resolve_run_skill(ctx, state)
        # skill is None only for an unconfirmed Explorer run (resolve_run_skill's
        # own docstring) -- no plan-grain result exists to count either way yet,
        # so [] (run_values' own explicit-empty fallback) is correct here, not a
        # forgotten argument. `build_run_table` itself skips the per-finding
        # merge when skill is None (its own docstring).
        catalogue_tests = catalogue_tests_for_skill(skill) if skill is not None else []
        return build_run_table(state, findings, metrics, skill=skill, catalogue_tests=catalogue_tests)
    if target_kind == "profile":
        _, table = build_profile_payload(state)
        return table
    if target_kind == "chart":
        # BUG-4b (independent review, 2026-09-25): a chart's own metric_names
        # table is not only "narrower" than every run metric -- for the
        # 'severity_distribution' chart it also carries run_high_count/
        # run_medium_count/run_low_count, computed ad hoc by
        # orchestrator.nodes.narration._chart_specs and NEVER persisted to
        # run_metrics. Offering "every run metric" alone therefore silently
        # DROPS names a caption legitimately cited and validated against at
        # generation time -- re-validation/render then fails N-G3 on
        # placeholders the model never invented. Reconstruct the SAME
        # per-chart table `orchestrator.narration.runner.narrate_captions`
        # built (`_chart_specs` + `build_caption_payload`, the identical two
        # calls `narrate()` makes), then still widen it with every run
        # metric -- this keeps the edit path's original, deliberate
        # permissiveness (N-H1 only checks that a typed number equals SOME
        # metric's rendered value, never that it was the metric the original
        # caption cited; G11's own per-task coverage requirement, N-C1, is
        # never applied to an edit) while fixing what re-validating a
        # genuinely model-authored placeholder needs.
        from orchestrator.narration.payloads import build_caption_payload
        from orchestrator.nodes.narration import _chart_specs

        metrics = ctx.persistence.get_run_metrics(state.run_id)
        findings_for_charts = ctx.persistence.list_findings(state.run_id)
        chart_specs, chart_metrics = _chart_specs(findings_for_charts, metrics)
        _, chart_tables = build_caption_payload(chart_specs, chart_metrics)
        table = dict(_metrics_placeholder_table(list(metrics), metrics))
        table.update(chart_tables.get(target_id, {}))
        return table
    raise NarrativeTargetNotFound(row["narrative_id"], target_kind, target_id)


def _narrative_allowed_identifiers(ctx: AppContext, state: RunState, row: dict) -> frozenset[str]:
    """The SAME N-D1 allow-list `orchestrator.narration.runner`/`candidates`
    built for this row's target AT GENERATION TIME (CLAUDE.md §3.3),
    reconstructed from this run's CURRENT persisted findings -- never a
    second, independently-maintained set. BUG-SYNTH-T1T2 (independent
    review round 3, 2026-09-25): `validate_prose` re-validated with NO
    allowed identifiers at all (the empty default), so a theme's stored
    root_cause/summary legitimately citing a member finding's own test_id
    (e.g. "T3.1a") -- valid at generation, where `narrate_synthesis`
    validated it against this run's full finding set -- failed on every
    later re-validation (the G11 invariant, an auditor's edit review, a
    live re-check). Only `theme`/`finding`/`candidate` targets carry any
    identifier allowance at generation; every other target (`run`, `chart`,
    `profile`) passes none there either, so this returns an empty set for
    them too -- narrower than `_narrative_table`'s catch-all, deliberately."""
    from orchestrator.narration.candidates import allowed_identifiers_for_cited_metrics
    from orchestrator.narration.payloads import identifiers_for_findings

    target_kind = row["target_kind"]
    if target_kind == "theme":
        # `narrate_synthesis` validates a theme's fields with `allowed`
        # built from EVERY finding passed to that one `find_synthesis` call
        # (orchestrator.nodes.narration's single `narrate_synthesis(rc,
        # findings, skill=skill)`, this run's full finding set) -- not only
        # the theme's own members, so the run-wide set is what a theme's
        # stored prose was actually validated against and must be
        # re-validated against too.
        return identifiers_for_findings(ctx.persistence.list_findings(state.run_id))
    if target_kind == "finding":
        finding = next(
            (f for f in ctx.persistence.list_findings(state.run_id) if f["finding_id"] == row["target_id"]), None
        )
        return identifiers_for_findings([finding]) if finding is not None else frozenset()
    if target_kind == "candidate":
        candidate = next(
            (c for c in ctx.persistence.list_candidates(state.run_id) if c["candidate_id"] == row["target_id"]), None
        )
        if candidate is None:
            return frozenset()
        skill = resolve_run_skill(ctx, state)
        test_ident: dict[str, dict] = {}
        if skill is not None:
            for test in skill.plan.get("tests", []):
                tid = test.get("test_id")
                if tid:
                    test_ident[tid] = {"control_id": test.get("control_id"), "risk_id": test.get("risk_id")}
        metrics = ctx.persistence.get_run_metrics(state.run_id)
        cited = candidate.get("metrics_cited") or []
        if isinstance(cited, dict):
            cited = list(cited)
        return frozenset(allowed_identifiers_for_cited_metrics(cited, metrics, test_ident))
    return frozenset()


def _canonical_json_compact(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def edit_narrative(ctx: AppContext, run_id: str, narrative_id: str, new_text, *, actor: str) -> dict:
    """§6.4 / CLAUDE.md §14 Answers Q3 (amended): an auditor may retype a
    model-written (or fallback) narrative field. Every typed number must
    equal the DISPLAYED rendering of a metric this item's own placeholder
    table carries (`validate_human_edit`, N-H1) -- every other §3.3-§3.4
    rule still applies (vague language, causation/policy language, length
    caps, ...). Allowed only while `awaiting_signoff` (§6.4: "After
    sign-off, no edits are possible"). Writes the G14 `narrative_edits` row
    BEFORE the current text is upserted, never after, so a crash between the
    two can never leave an edit applied but unrecorded."""
    from orchestrator.narration.placeholders import render, scan_placeholders
    from orchestrator.narration.validate import validate_human_edit

    state = ctx.persistence.load_state(run_id)
    if state.status != "awaiting_signoff":
        raise NarrativeEditNotAllowed(narrative_id, state.status)
    _refuse_unless_preparation_stage(ctx, state, "narrative_edited", actor)

    row = next((r for r in ctx.persistence.get_narratives(run_id) if r["narrative_id"] == narrative_id), None)
    if row is None:
        raise NarrativeNotFound(narrative_id)

    field = row["field"]
    validator_field = _VALIDATOR_FIELD_FOR.get((row["target_kind"], field))
    if validator_field is None:
        raise NarrativeTargetNotFound(narrative_id, row["target_kind"], field)

    table = _narrative_table(ctx, state, row)

    is_list = field in _LIST_NARRATIVE_FIELDS
    if is_list:
        items = list(new_text) if not isinstance(new_text, str) else [new_text]
    else:
        if not isinstance(new_text, str):
            raise ValueError(f"narrative {narrative_id!r} field {field!r} takes a single string, not {type(new_text)!r}")
        items = [new_text]

    violations: list = []
    for item in items:
        result = validate_human_edit(item, table, field=validator_field)
        violations.extend(result.violations)
    if violations:
        raise NarrativeEditRejected(
            narrative_id, [{"rule_id": v.rule_id, "message": v.message, "text": v.text} for v in violations]
        )
    for item in items:
        render(item, table)  # defensive: validate_human_edit must guarantee this succeeds (CLAUDE.md NN14)

    used: set[str] = set()
    for item in items:
        for span in scan_placeholders(item):
            if span.valid_syntax and span.name in table:
                used.add(span.name)
    sources = [
        {"placeholder": f"{{{table[n].cls}:{n}}}", "source_field": table[n].source_field, "unit": table[n].unit}
        for n in sorted(used)
    ]
    template_text = _canonical_json_compact(items) if is_list else items[0]

    now = ctx.clock()
    new_version = int(row["version"]) + 1
    before_text = row.get("template_text")
    diff = "\n".join(
        difflib.unified_diff(
            (before_text or "").splitlines(keepends=True),
            (template_text or "").splitlines(keepends=True),
            lineterm="",
        )
    )
    new_row = {
        "narrative_id": narrative_id, "run_id": run_id, "engagement_id": row.get("engagement_id"),
        "target_kind": row["target_kind"], "target_id": row["target_id"], "field": field,
        "version": new_version, "generation": row.get("generation", 0), "origin": "human_edit",
        "template_text": template_text, "sources": sources, "call_ids": [],
        "served_model_version": None, "violations": None, "updated_by": actor, "updated_at": now,
    }
    # P6 WP N10 (§6.4): a real CAS -- the conditional write applies only if
    # `narratives.version` still equals the version this call READ at the top
    # (`row["version"]`); a racing edit or a `narrate()` regenerate that
    # landed first makes this return False, refused rather than silently
    # overwritten (CLAUDE.md NN14). Applied BEFORE the `narrative_edits` row
    # below (the reverse of this function's earlier ordering, which wrote the
    # edit record first): recording a G14 edit whose `before_text` no longer
    # matches what was actually there would be a WRONG audit trail on every
    # conflict, not merely an occasionally-missing one on a rare crash
    # between the two writes -- the worse failure mode of the two.
    if not ctx.persistence.upsert_narrative(new_row, expected_version=row["version"]):
        raise NarrativeEditConflict(narrative_id, row["version"], None)

    edit_id = hashlib.sha256(f"{narrative_id}|{new_version}".encode("utf-8")).hexdigest()[:32]
    ctx.persistence.append_narrative_edit(
        {
            "edit_id": edit_id, "narrative_id": narrative_id, "run_id": run_id, "version": new_version,
            "origin": "human_edit", "action": "human_edit", "actor": actor, "at": now,
            "before_text": before_text, "after_text": template_text, "diff": diff,
            "reason": None, "call_id": None,
        }
    )

    _emit_service_event(
        ctx.persistence, state,
        event_id=_service_trace_event_id(run_id, narrative_id, str(new_version), "narrative_edited"),
        event_type="narrative_edited", actor=actor,
        message=f"{narrative_id} ({row['target_kind']}.{field}) edited by {actor} (v{new_version})", now=now,
    )
    return new_row


def regenerate_narration(ctx: AppContext, run_id: str, actor: str) -> RunState:
    """§5.5: re-runs only `narrate` and `act` (via `transition(restart_at_
    index=...)`, orchestrator.status/orchestrator.runs.regenerate_narration).
    Rule findings, their numbers and their severities are untouched -- only
    `find`/`prioritise` decide those, and neither runs again. Requires
    `NARRATION_ENABLED` and the run to be `awaiting_signoff`."""
    if not getattr(ctx.settings, "narration_enabled", False):
        raise NarrationDisabled(run_id)
    state = ctx.persistence.load_state(run_id)
    if state.status != "awaiting_signoff":
        raise RunNotAwaitingSignoff(run_id, state.status)
    _refuse_unless_preparation_stage(ctx, state, "narration_regenerated", actor)

    node_names = [name for name, _ in NODES_FOR.get(state.run_kind, {}).get(state.phase, [])]
    try:
        narrate_index = node_names.index("narrate")
    except ValueError:
        raise NarrationNodeUnavailable(run_id, state.run_kind, state.phase)

    new_state = runs_module.regenerate_narration(
        ctx.persistence, run_id, actor=actor, now=ctx.clock(), narrate_node_index=narrate_index,
    )
    if ctx.executor is not None:
        ctx.executor.start(run_id, new_state.phase)
    return new_state


def resume_run(ctx: AppContext, run_id: str, actor: str) -> RunState:
    current_state = ctx.persistence.load_state(run_id)
    fingerprint = build_run_fingerprint(ctx, current_state)
    state = runs_module.resume(
        ctx.persistence, run_id, actor=actor, now=ctx.clock(), current_fingerprint=fingerprint
    )
    if ctx.executor is not None:
        ctx.executor.start(run_id, state.phase)
    return state


# ── Evidence / results ───────────────────────────────────────────────────────


def get_run_payload(ctx: AppContext, run_id: str, *, state: RunState | None = None) -> dict:
    """The evidence payload, shaped like reference_app/app.py's
    compute_evidence_payload (CLAUDE.md build brief P3 §4) but built entirely
    from what the run already persisted (run_metrics, findings,
    reconciliation) -- never recomputed from raw source data here.

    `state`, when given, is a RunState the caller already loaded for this
    same run_id -- skips a second persistence.load_state round trip. See
    get_run's own docstring for the pattern; app/src/platform/adapters.
    get_run_payload_and_frames is the caller that shares one load across
    get_run_payload/get_run_frames for /workspace/tne's _load_bundle (P3/P4
    perf gap review 2026-09-25)."""
    if state is None:
        state = ctx.persistence.load_state(run_id)
    metrics = ctx.persistence.get_run_metrics(run_id)
    findings = ctx.persistence.list_findings(run_id)

    metrics_payload = {}
    for name, m in metrics.items():
        sources = (m.get("source_ref") or {}).get("sources", [])
        metrics_payload[name] = {
            "value": m["value"],
            "unit": m.get("unit"),
            "source_file": sources[0]["name"] if sources else None,
            "source_ref": m.get("source_ref", {}),
        }

    not_testable = [t for t in state.test_results if t["status"] == "not_testable"]
    exposure_metric = metrics.get("run_exposure_headline")
    approved_not_spent_metric = metrics.get("run_approved_not_spent_total")

    # P6 §5.3: "between a sign-off that accepted at least one candidate and
    # `finalise` completing, get_run_payload returns exposure.pending_
    # recompute = true" -- so the UI can show "—" rather than a headline
    # that is about to change under it (CLAUDE.md §11 "'—' replaces a
    # fabricated '$0'"). True exactly while an accepted candidate has not
    # yet been copied into `findings` (origin='ai_proposed', WP N10's
    # `finalise`) -- never true before sign-off, because a candidate cannot
    # be 'accepted' before then (decide_candidate requires awaiting_signoff).
    pending_recompute = False
    if state.signoff is not None:
        accepted_ids = {c["candidate_id"] for c in ctx.persistence.list_candidates(run_id) if c["candidate_status"] == "accepted"}
        if accepted_ids:
            finalised_ids = {f["candidate_id"] for f in findings if f.get("origin") == "ai_proposed" and f.get("candidate_id")}
            pending_recompute = bool(accepted_ids - finalised_ids)

    return {
        "run_id": run_id,
        "status": state.status,
        "audit_period": f"{state.audit_period[0]} to {state.audit_period[1]}",
        "metrics": metrics_payload,
        "test_results": state.test_results,
        "not_testable": not_testable,
        "findings": findings,
        "reconciliation": state.reconciliation,
        # B2 (CLAUDE.md P2/P3 gate review): "headline" is the "Gross value of
        # flagged spend (de-duplicated)" figure -- never call it "exposure" in
        # a UI label, that word implies every dollar is at risk, which
        # 'excess'-basis findings (only the over-limit portion) contradict.
        # approved_not_spent is reported alongside it, never inside it.
        "exposure": {
            "headline": exposure_metric["value"] if exposure_metric else None,
            "basis": (exposure_metric.get("source_ref") or {}).get("basis") if exposure_metric else None,
            "label": (exposure_metric.get("source_ref") or {}).get("label") if exposure_metric else None,
            "sources": (exposure_metric.get("source_ref") or {}).get("sources") if exposure_metric else None,
            "approved_not_spent_total": approved_not_spent_metric["value"] if approved_not_spent_metric else None,
            "pending_recompute": pending_recompute,
        },
    }


def get_run_frames(ctx: AppContext, run_id: str, *, state: RunState | None = None) -> dict[str, pd.DataFrame]:
    """Row-level frames for /workspace/tne: one DataFrame per contract source
    this run bound, holding that source's own contract-typed columns
    (__source, __row_key join keys included) plus one 0/1 int column per RF_*
    flag this run produced against that source -- <NA> (nullable Int64) for a
    not_testable test's declared flag, never a guessed 0. See FRAME_COLUMNS
    (populated per Skill on first call) for the exact column list.

    Reads the per-run row snapshots the `execute` node wrote
    (state.exports["frames"], orchestrator/frames.py, CLAUDE.md build brief P4
    perf fix) -- a few small Parquet files already restricted to the rows
    this run's populations actually tested, with their RF_* flag columns
    already baked in -- instead of re-reading every full bound source (the
    ~39s deployed / ~64s local callback this replaces). Every snapshot's
    bytes are verified against its recorded sha256 before use; a mismatch
    raises rather than silently re-reading the source (CLAUDE.md NN14).

    A run completed BEFORE this change recorded no snapshot (state.exports
    has no "frames" key), so this falls back to the old path: read every
    bound source at its PINNED version (state.data_assets) and pivot
    flagged_rows into RF_* columns live. That fallback is logged -- it is the
    slow path this function exists to avoid, kept only for runs that predate
    the fix.

    `state`, when given, is a RunState the caller already loaded for this
    same run_id (see get_run_payload's own docstring -- same P3/P4 perf gap
    review 2026-09-25 pattern, app/src/platform/adapters.
    get_run_payload_and_frames)."""
    if state is None:
        state = ctx.persistence.load_state(run_id)
    skill = resolve_run_skill(ctx, state)

    frame_exports = (state.exports or {}).get("frames")
    if frame_exports:
        frames: dict[str, pd.DataFrame] = {}
        for source, meta in frame_exports.items():
            df = read_frame_parquet(
                ctx.export_storage, source=source, path=meta["path"], expected_sha256=meta["sha256"],
            )
            frames[source] = df
            FRAME_COLUMNS[source] = tuple(df.columns)
        return frames

    _LOG.warning(
        "get_run_frames(%s): no frame snapshot recorded (a pre-snapshot run) -- "
        "falling back to a full re-read of every bound source",
        run_id,
    )
    return _get_run_frames_from_sources(ctx, state, skill)


def _get_run_frames_from_sources(ctx: AppContext, state: RunState, skill) -> dict[str, pd.DataFrame]:
    """The pre-snapshot get_run_frames path, kept only as the fallback for a
    run that completed before frame snapshots existed (see get_run_frames'
    own docstring)."""
    bindings = {b["source"]: b["table_fqn"] for b in state.data_assets}
    versions = {b["source"]: b["version"] for b in state.data_assets}
    data_source = ctx.data_source_factory(bindings, skill.contract.get("sources", {}), state.skill_id)

    # CLAUDE.md §0.5/NN14: an audit period is a business-calendar concept in a
    # stated timezone. This fallback path must thread the contract's declared
    # timezone through read_population exactly as engine.py's execute_skill
    # does, and fail loudly rather than let a Skill built without one silently
    # fall back to a guess.
    audit_timezone = skill.contract.get("timezone")
    if not audit_timezone:
        raise ContractViolation(
            ["contract missing required 'timezone' -- an audit period is a business-calendar "
             "concept and cannot be evaluated without one (CLAUDE.md §0.5, NN14)"]
        )

    flagged_rows = ctx.persistence.list_flagged_rows(state.run_id)
    by_source: dict[str, list[dict]] = {}
    for r in flagged_rows:
        by_source.setdefault(r["source"], []).append(r)

    nt_flags = not_testable_flags(skill)

    try:
        frames: dict[str, pd.DataFrame] = {}
        for source in skill.contract.get("sources", {}):
            version = versions.get(source)
            if version is None:
                continue
            df = data_source.read_population(source, version=version, audit_timezone=audit_timezone)

            rows = by_source.get(source, [])
            flags_present = sorted({r["flag"] for r in rows})
            for flag in flags_present:
                keys = {r["row_key"] for r in rows if r["flag"] == flag}
                df[flag] = df["__row_key"].isin(keys).astype("Int64")
            for flag in nt_flags:
                if flag not in df.columns:
                    df[flag] = pd.array([pd.NA] * len(df), dtype="Int64")

            frames[source] = df
            FRAME_COLUMNS[source] = tuple(df.columns)

        return frames
    finally:
        _close_data_source(data_source)


# ── Uploaded files (build brief P5) ────────────────────────────────────────

_DEFAULT_MAX_UPLOAD_MB = 100


def get_upload_base_path(ctx: AppContext) -> str:
    """The real configured destination for uploads -- DBX_VOLUME (via
    ctx.settings.volume) for the UC backend, the local export root for the
    local backend. Never a hardcoded workspace-specific Volume path
    (CLAUDE.md §0.2/NN16 -- the prototype's fixed _UPLOAD_BASE)."""
    if ctx.backend == "local":
        return str(ctx.export_storage.root_dir)
    if not ctx.settings.volume:
        from orchestrator.errors import ConfigError

        raise ConfigError("DBX_VOLUME is not configured -- uploads have nowhere to go")
    return ctx.settings.volume


# CLAUDE.md independent review 2026-09-24, item 4: an upload's filename is
# person-supplied and untrusted. `Path(filename).name` alone strips any
# directory component (so "../../etc/passwd" or "/etc/passwd" cannot walk
# out of uploads/<upload_id>/), and this allow-list additionally rejects
# anything that is not an ordinary filename character -- never silently
# transliterated or truncated, since either could collide two different
# uploaded filenames onto the same destination path.
_SAFE_UPLOAD_FILENAME_CHARS = set(string.ascii_letters + string.digits + "._- ()")


def _sanitise_upload_filename(filename: str) -> str:
    name = Path(filename).name
    if not name or name in (".", ".."):
        raise ValueError(f"{filename!r} is not a valid upload filename")
    if not set(name) <= _SAFE_UPLOAD_FILENAME_CHARS:
        bad = sorted(set(name) - _SAFE_UPLOAD_FILENAME_CHARS)
        raise ValueError(f"{filename!r} contains character(s) not allowed in an upload filename: {bad}")
    return name


def _profile_uploaded_bytes(filename: str, content: bytes) -> tuple[int, list[str]]:
    """Parses the file to get a row count and column list (CLAUDE.md build
    brief P5: 'profiling = parse CSV/XLSX/Parquet, row count + columns').
    Raises on anything unparseable -- caught by the caller and recorded as a
    Failed upload with the real error, never a silent partial result."""
    import io

    lower = filename.lower()
    if lower.endswith(".csv"):
        df = pd.read_csv(io.BytesIO(content))
    elif lower.endswith((".xlsx", ".xls")):
        df = pd.read_excel(io.BytesIO(content))
    elif lower.endswith(".parquet"):
        df = pd.read_parquet(io.BytesIO(content))
    else:
        raise ValueError(f"unsupported file type: {filename!r} (expected .csv, .xlsx or .parquet)")
    return len(df), [str(c) for c in df.columns]


def upload_file(
    ctx: AppContext,
    *,
    filename: str,
    content: bytes,
    uploaded_by: str,
    engagement_id: str = "ENG-DEFAULT",
) -> dict:
    """Writes `content` to the configured Volume/local export root at
    uploads/<upload_id>/<sanitised filename>, records an `uploaded_files`
    row, and profiles it inline (CSV/XLSX/Parquet -> row_count +
    columns_json). Status moves Uploaded -> Profiling -> Ready|Failed; a
    profiling failure never becomes a silent 0-row success (CLAUDE.md
    NN14). `filename` is untrusted input -- see _sanitise_upload_filename."""
    import hashlib
    import json
    import uuid as _uuid

    max_mb = float(os.environ.get("MAX_UPLOAD_MB") or _DEFAULT_MAX_UPLOAD_MB)
    size_bytes = len(content)
    if size_bytes > max_mb * 1_048_576:
        raise ValueError(
            f"{filename!r} is {size_bytes / 1_048_576:.1f} MB, over the configured "
            f"{max_mb:.0f} MB upload limit (MAX_UPLOAD_MB)"
        )

    upload_id = f"UP-{_uuid.uuid4().hex[:12]}"
    sha256 = hashlib.sha256(content).hexdigest()
    safe_filename = _sanitise_upload_filename(filename)
    dest_path = f"uploads/{upload_id}/{safe_filename}"
    if ctx.export_storage.exists(dest_path):
        # upload_id is a fresh uuid4 per call, so a collision here would mean
        # something else already wrote this exact path -- never silently
        # overwrite whatever that is (CLAUDE.md NN14, independent review
        # 2026-09-24 item 4).
        raise ValueError(f"upload destination {dest_path!r} already exists -- refusing to overwrite it")
    volume_path = ctx.export_storage.write(dest_path, content)
    now = ctx.clock()

    row = {
        "upload_id": upload_id,
        "engagement_id": engagement_id,
        "filename": filename,
        "volume_path": volume_path,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "uploaded_by": uploaded_by,
        "uploaded_at": now,
        "status": "Uploaded",
        "row_count": None,
        "columns_json": None,
        "error": None,
    }
    ctx.persistence.record_uploaded_file(row)
    ctx.persistence.update_uploaded_file(upload_id, status="Profiling")

    try:
        row_count, columns = _profile_uploaded_bytes(filename, content)
    except Exception as exc:
        ctx.persistence.update_uploaded_file(upload_id, status="Failed", error=str(exc))
        row["status"] = "Failed"
        row["error"] = str(exc)
        return row

    columns_json = json.dumps(columns)
    ctx.persistence.update_uploaded_file(
        upload_id, status="Ready", row_count=row_count, columns_json=columns_json
    )
    row["status"] = "Ready"
    row["row_count"] = row_count
    row["columns_json"] = columns_json
    return row


def list_uploaded_files(ctx: AppContext, engagement_id: str | None = None) -> list[dict]:
    return ctx.persistence.list_uploaded_files(engagement_id)


# ── Explorer/Playbook workflow preview ───────────────────────────────────────


def propose_plan(ctx: AppContext, *, skill_id: str, mode: str = "playbook") -> dict:
    """The real node sequence for `mode` (CLAUDE.md §2.4/§4.2 NODES_FOR),
    matching the shape the landing page's workflow preview renders --
    {"stages": [{"stage", "status", "detail"}], "mode", "mock": False}.
    Never fabricated: only the first two stages (source binding, which this
    run's real suggest_bindings resolves; and the Skill/Explorer plan step,
    which is `mode` itself) can honestly be called 'ready' before any node
    has actually executed -- every later stage is genuinely 'pending'."""
    # BUG-STARTRUN-1: `skill` threaded into suggest_bindings rather than it
    # re-fetching the same skill_id (see that function's own docstring).
    skill = get_skill(ctx, skill_id) if skill_id else None
    bindings = suggest_bindings(ctx, skill_id, skill=skill) if skill_id else {}
    bound_count = sum(1 for v in bindings.values() if v)
    tests = (skill.get("tests") if skill else None) or []
    test_count = len(tests) if isinstance(tests, list) else tests

    stages = [
        {"stage": "Source data", "status": "ready" if bound_count == len(bindings) and bindings else "pending",
         "detail": f"{bound_count} of {len(bindings)} sources bound" if bindings else "No sources declared"},
        {"stage": "Data quality & reconciliation", "status": "pending"},
        {"stage": "Skill / Explorer plan",
         "status": "ready" if mode == "playbook" else "needs_confirmation",
         "detail": skill["name"] if skill and mode == "playbook" else "Auditor confirmation required"},
        {"stage": "Deterministic audit tests", "status": "pending",
         "detail": f"{test_count if test_count else '?'} tests defined"},
        {"stage": "Exception classification", "status": "pending"},
        {"stage": "Evidence-linked findings", "status": "pending"},
        {"stage": "Insights & prioritisation", "status": "pending"},
        {"stage": "Management actions", "status": "pending"},
        {"stage": "Export & Jira preview", "status": "pending"},
    ]
    return {"stages": stages, "mode": mode, "mock": False}


def get_export(ctx: AppContext, run_id: str, kind: str) -> tuple[str, bytes]:
    exports = ctx.persistence.list_exports(run_id)
    match = next((e for e in exports if e["kind"] == kind), None)
    if match is None:
        raise FileNotFoundError(f"no {kind!r} export recorded for run {run_id!r}")
    content = ctx.export_storage.read(match["path"])
    # Independent review 2026-09-24 item 4: verify against the sha256
    # recorded at write time (record_export -- every export writer, incl.
    # an upload-derived one, records this) before handing bytes back to a
    # download callback -- a mismatch fails loudly rather than silently
    # serving content that no longer matches this run's own evidence
    # (CLAUDE.md NN14), same pattern as orchestrator.frames.read_frame_parquet.
    expected_sha256 = match.get("sha256")
    if expected_sha256:
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if actual_sha256 != expected_sha256:
            raise ValueError(
                f"export {kind!r} for run {run_id!r} failed integrity check: recorded "
                f"sha256={expected_sha256!r}, bytes read back hash to {actual_sha256!r}"
            )
    filename = Path(match["path"]).name
    return filename, content


# ── health ────────────────────────────────────────────────────────────────────


def health(ctx: AppContext) -> dict:
    return {"status": "ok"}


def ready(ctx: AppContext) -> dict:
    # /ready is typically reached without auth (a platform health check), so
    # the exception's own text (which can carry a connection string, a table
    # name, or other internal detail) is logged server-side only -- the
    # caller gets a generic status, never repr(exc) (CLAUDE.md P2/P3 gate
    # review item 7). Independent review 2026-09-24 item 5: when a cached
    # readiness prober is wired (every real deployment), it also checks the
    # Volume, configured source bindings and model endpoints, and the whole
    # report is cached for readiness_cache_ttl_s -- re-probing the
    # warehouse/Volume/model endpoints on every poll is exactly the shape of
    # the §11 idle-cost incident. A caller with no `ctx.readiness` (an older
    # direct AppContext(...) construction, e.g. in tests) falls back to the
    # original warehouse-only check so nothing that worked before regresses.
    if ctx.readiness is not None:
        report = ctx.readiness.get()
        payload = report.as_dict()
        payload["backend"] = ctx.backend
        return payload

    try:
        ctx.persistence.find_runs(["queued"])
        db_ok = True
        detail = None
    except Exception:
        db_ok = False
        detail = "database connectivity check failed"
        _LOG.exception("service.ready(): database connectivity check failed")
    return {"ready": db_ok, "backend": ctx.backend, "detail": detail}


def readiness_report(ctx: AppContext, *, force: bool = False) -> dict:
    """Independent review 2026-09-24 item 5: the same readiness report
    `ready()` returns, but explicitly forceable (bypassing the cache) --
    used by start_audit_run's pre-flight check, and callable directly by an
    operator/test that wants a fresh read rather than whatever the cache
    happens to hold."""
    if ctx.readiness is None:
        return {"ready": True, "checks": [], "checked_at": None}
    return ctx.readiness.get(force=force).as_dict()
