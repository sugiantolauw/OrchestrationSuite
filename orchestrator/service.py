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
    list_trace_events(ctx, run_id=None) -> list[dict]
    list_management_actions(ctx, filters=None) -> list[dict]
    confirm_plan(ctx, run_id, actor) -> RunState
    sign_off(ctx, run_id, actor) -> RunState
    resume_run(ctx, run_id, actor) -> RunState
    get_run_payload(ctx, run_id) -> dict
    get_run_frames(ctx, run_id) -> dict[str, pandas.DataFrame]
    get_export(ctx, run_id, kind) -> tuple[str, bytes]
    get_upload_base_path(ctx) -> str
    upload_file(ctx, *, filename, content, uploaded_by, engagement_id='ENG-DEFAULT') -> dict
    list_uploaded_files(ctx, engagement_id=None) -> list[dict]
    propose_plan(ctx, *, skill_id, mode='playbook') -> dict
    health(ctx) -> dict
    ready(ctx) -> dict

`ORCH_BACKEND=local` (env) selects LocalPersistence + a local-file
DataSourceAdapter for local/E2E runs (against synthetic_data/ by default) --
surfaced everywhere as data_mode "Local test data". Anything else selects
DeltaPersistence + UCTableDataSource (orchestrator/adapters/datasource_uc.py,
owned by the Unity Catalog work) against Unity Catalog, surfaced as
"Unity Catalog".
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import string
import uuid
from dataclasses import dataclass, field
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
from orchestrator.executor import ThreadExecutor
from orchestrator.fingerprint import compute_fingerprint
from orchestrator.frames import not_testable_flags, read_frame_parquet
from orchestrator.nodes.context import NodeContext
from orchestrator.nodes.fieldwork import NODES_FOR
from orchestrator.pipeline import NODE_STAGE_LABELS
from orchestrator.signoff_policy import SOD_ENFORCED, evaluate_signoff
from orchestrator.skill_registry import register_skill
from orchestrator.skills import load_skill, plan_test_flags
from orchestrator.state import RunState, to_json
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
    data_source_factory: Callable[[dict[str, str], dict[str, dict] | None], Any]
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

    def factory(bindings: dict[str, str], contract_sources: dict[str, dict] | None = None):
        sources: dict[str, dict] = {}
        for name in bindings:
            cfg = dict(configs.get(name) or {})
            file_override = bindings.get(name)
            if file_override:
                cfg["file"] = file_override
            sources[name] = cfg
        return LocalFileDataSource(root_dir=data_root, sources=sources)

    return factory


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
    skill_dir = _skill_dir_for(ctx, state.skill_id)
    skill = load_skill(skill_dir)
    bindings = {b["source"]: b["table_fqn"] for b in state.data_assets}
    data_source = ctx.data_source_factory(bindings, skill.contract.get("sources", {}))
    return NodeContext(
        settings=ctx.settings,
        persistence=ctx.persistence,
        data_source=data_source,
        skill=skill,
        clock=ctx.clock,
        export_storage=ctx.export_storage,
    )


def build_run_fingerprint(ctx: AppContext, state: RunState) -> dict:
    skill_dir = _skill_dir_for(ctx, state.skill_id)
    pinned_versions = {b["source"]: b["version"] for b in state.data_assets}
    # Same reconstruction as start_audit_run: a binding whose value is a
    # recorded upload's volume_path is pinned as an uploaded_file_hash too,
    # so a resumed/verified run's recomputed fingerprint matches the one
    # captured at creation (CLAUDE.md §4.1 "verified at every executor pass").
    uploaded_volume_paths = {r["volume_path"] for r in ctx.persistence.list_uploaded_files()}
    uploaded_file_hashes = {
        b["table_fqn"]: b["version"] for b in state.data_assets if b["table_fqn"] in uploaded_volume_paths
    }
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

        def _uc_factory(bindings: dict[str, str], contract_sources: dict[str, dict] | None = None,
                        _settings=settings, _persistence=persistence):
            try:
                from orchestrator.adapters.datasource_uc import UCTableDataSource
            except ImportError as exc:  # pragma: no cover - depends on a sibling agent's file
                from orchestrator.errors import ConfigError

                raise ConfigError(
                    "orchestrator.adapters.datasource_uc.UCTableDataSource is not available "
                    "yet -- the Unity Catalog source-data work has not landed in this checkout"
                ) from exc
            table_source = UCTableDataSource(_settings, bindings)
            # A source bound to an uploaded file's Volume path (rather than a
            # UC table FQN) is read from the Volume via the Files API, not
            # sent through the SQL path that a Volume path cannot satisfy
            # (CLAUDE.md §5 UI item 5). Wrapping unconditionally costs
            # nothing when no binding is actually an upload -- every call
            # just falls through to `table_source` unchanged.
            from orchestrator.adapters.datasource_volume_upload import VolumeUploadAwareDataSource

            return VolumeUploadAwareDataSource(
                table_source=table_source,
                bindings=bindings,
                contract_sources=contract_sources or {},
                persistence=_persistence,
                export_storage=export_storage,
            )

        export_storage = VolumeExportStorage(volume_root=settings.volume) if settings.volume else None
        ctx = AppContext(
            settings=settings, persistence=persistence, skills_dir=skills_dir,
            data_source_factory=_uc_factory, export_storage=export_storage,
            clock=clock, backend="uc", tracing=tracing,
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


def list_skills(ctx: AppContext) -> list[dict]:
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

        runs_rows = ctx.persistence.list_runs(filters={"skill_id": skill_id})
        completed = [r for r in runs_rows if r.get("status") == "completed"]
        last_run = runs_rows[0]["created_at"] if runs_rows else None

        versions = ctx.persistence.list_skill_versions(skill_id)
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
        return None
    base = next((e for e in list_skills(ctx) if e["skill_id"] == skill_id), None)
    if base is None:
        return None

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
    list_tables = getattr(data_source, "list_tables", None)
    if list_tables is None:
        from orchestrator.errors import ConfigError

        raise ConfigError(
            "UCTableDataSource.list_tables() is not available -- the Unity Catalog source-data "
            "work has not landed in this checkout yet"
        )
    return list_tables()


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

    cards = []
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
        if limit is not None and data_source is not None and not t.get("restricted") and t.get("fqn"):
            fqn = t["fqn"]
            card["rows"] = data_source.get_row_count(fqn)
            classification = data_source.get_classification(fqn)
            if classification:
                card["classification"] = classification
        cards.append(card)
    return cards


def suggest_bindings(ctx: AppContext, skill_id: str) -> dict[str, str | None]:
    skill = get_skill(ctx, skill_id)
    if skill is None:
        return {}
    source_names = [s["source"] for s in skill["sources"]]

    if ctx.backend == "local":
        configs = _all_skill_source_configs(ctx.skills_dir)
        return {name: (configs.get(name) or {}).get("file") for name in source_names}

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
    return {name: by_short_name.get(name) for name in source_names}


# ── Runs ──────────────────────────────────────────────────────────────────────


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
) -> str:
    skill_dir = _skill_dir_for(ctx, skill_id)
    skill = load_skill(skill_dir)
    skill.validate()

    contract_sources = skill.contract.get("sources", {})
    missing = sorted(set(contract_sources) - set(bindings))
    if missing:
        raise ContractViolation([f"no binding supplied for contract source {s!r}" for s in missing])

    data_source = ctx.data_source_factory(bindings, contract_sources)

    # Resolve every source's version FIRST, before any read (CLAUDE.md §4.1
    # TOCTOU ordering) -- these become both this run's pinned data_assets
    # bindings and the fingerprint's source_table_versions.
    source_versions = {name: data_source.resolve_version(name) for name in contract_sources}

    # A source bound to an uploaded file (run_setup._auto_bind's exact-
    # filename-stem match) is pinned in run_fingerprints.uploaded_file_hashes
    # ({volume_path: sha256}, CLAUDE.md §4.1 P1A) as well as in
    # source_table_versions above -- the fingerprint records not just WHAT
    # version was read but that it came from a business-provided file, not a
    # governed table.
    uploaded_volume_paths = {r["volume_path"] for r in ctx.persistence.list_uploaded_files()}
    uploaded_file_hashes = {
        bindings[name]: source_versions[name]
        for name in contract_sources
        if bindings[name] in uploaded_volume_paths
    }

    now = ctx.clock()
    fingerprint = _compute_run_fingerprint(ctx, skill_dir, source_versions, uploaded_file_hashes)

    register_skill(skill, ctx.persistence, actor=run_owner, now=now)

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


def _queue_affinity_note(ctx: AppContext, status: str, fingerprint_id: str | None) -> str | None:
    """P3 gate review item 4: a run created by a different deployment is left
    `queued` untouched rather than claimed and failed at fingerprint
    verification (ThreadExecutor's own admission-time check,
    orchestrator/executor.py -- the SAME code_revision comparison, so a run
    this note names is exactly one that executor would also skip). Surfaced
    here, read-only, so the UI can show WHY a queued run is not progressing.
    Returns None whenever there is nothing to say -- not queued, this
    deployment's own code_revision is not configured (dev/local, where every
    run is presumed this deployment's own), or the fingerprint row cannot be
    read."""
    if status != "queued":
        return None
    own_code_revision = getattr(ctx.settings, "code_revision", None)
    if own_code_revision is None or not fingerprint_id:
        return None
    try:
        stored_fingerprint = ctx.persistence.get_fingerprint(fingerprint_id)
    except Exception:
        return None
    stored_code_revision = (stored_fingerprint or {}).get("code_revision")
    if stored_code_revision and stored_code_revision != own_code_revision:
        return f"queued — created by a different deployment (code revision {stored_code_revision[:12]})"
    return None


def get_run(ctx: AppContext, run_id: str) -> dict:
    state = ctx.persistence.load_state(run_id)
    payload = json.loads(to_json(state))
    payload["status_label"] = state.status.replace("_", " ").title()
    payload["queue_note"] = _queue_affinity_note(ctx, state.status, state.fingerprint_id)

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


def list_runs(ctx: AppContext, filters: dict | None = None) -> list[dict]:
    rows = ctx.persistence.list_runs(filters=filters)
    skills_by_id = {e["skill_id"]: e for e in list_skills(ctx)}
    data_mode = "Local test data" if ctx.backend == "local" else "Unity Catalog"

    out = []
    for r in rows:
        run_id = r["run_id"]
        findings = ctx.persistence.list_findings(run_id)
        actions = ctx.persistence.list_management_actions(filters={"run_id": run_id})
        open_actions = [a for a in actions if a.get("status") != "closed"]
        exposure_metric = ctx.persistence.get_run_metrics(run_id).get("run_exposure_headline")
        skill_entry = skills_by_id.get(r.get("skill_id"))

        # approved_by (runs.approved_by, populated by orchestrator.runs.sign_off
        # via persistence) is the same `actor` string evaluate_signoff compared
        # against run_owner at sign-off time -- re-deriving self_approved from
        # these two already-projected columns is exactly that same equality,
        # not a second policy decision (CLAUDE.md §11 self sign-off decision;
        # orchestrator/signoff_policy.py is the single source of the rule).
        approved_by = r.get("approved_by")
        self_approved = bool(approved_by) and evaluate_signoff(
            actor=approved_by, run_owner=r["run_owner"]
        )["self_approved"]

        out.append(
            {
                "run_id": run_id,
                "skill_id": r.get("skill_id"),
                "engagement_id": r.get("engagement_id"),
                "skill_name": skill_entry["name"] if skill_entry else r.get("skill_id"),
                "audit_period": f"{r['audit_period_start']} – {r['audit_period_end']}",
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
                "sod_enforced": SOD_ENFORCED,
                "queue_note": _queue_affinity_note(ctx, r["status"], r.get("fingerprint_id")),
            }
        )
    return out


def list_trace_events(ctx: AppContext, run_id: str | None = None) -> list[dict]:
    return ctx.persistence.list_trace_events(run_id)


def list_management_actions(ctx: AppContext, filters: dict | None = None) -> list[dict]:
    rows = ctx.persistence.list_management_actions(filters=filters)
    skills_by_id = {e["skill_id"]: e for e in list_skills(ctx)}
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
            }
        )
    return out


def confirm_plan(ctx: AppContext, run_id: str, actor: str) -> RunState:
    state = runs_module.confirm_plan(ctx.persistence, run_id, actor=actor, now=ctx.clock())
    if ctx.executor is not None:
        ctx.executor.start(run_id, state.phase)
    return state


def sign_off(ctx: AppContext, run_id: str, actor: str) -> RunState:
    state = runs_module.sign_off(ctx.persistence, run_id, actor=actor, now=ctx.clock())
    if ctx.executor is not None:
        ctx.executor.start(run_id, state.phase)
    return state


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


def get_run_payload(ctx: AppContext, run_id: str) -> dict:
    """The evidence payload, shaped like reference_app/app.py's
    compute_evidence_payload (CLAUDE.md build brief P3 §4) but built entirely
    from what the run already persisted (run_metrics, findings,
    reconciliation) -- never recomputed from raw source data here."""
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
        },
    }


def get_run_frames(ctx: AppContext, run_id: str) -> dict[str, pd.DataFrame]:
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
    the fix."""
    state = ctx.persistence.load_state(run_id)
    skill_dir = _skill_dir_for(ctx, state.skill_id)
    skill = load_skill(skill_dir)

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
    data_source = ctx.data_source_factory(bindings, skill.contract.get("sources", {}))

    flagged_rows = ctx.persistence.list_flagged_rows(state.run_id)
    by_source: dict[str, list[dict]] = {}
    for r in flagged_rows:
        by_source.setdefault(r["source"], []).append(r)

    nt_flags = not_testable_flags(skill)

    frames: dict[str, pd.DataFrame] = {}
    for source in skill.contract.get("sources", {}):
        version = versions.get(source)
        if version is None:
            continue
        df = data_source.read_population(source, version=version)

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
    skill = get_skill(ctx, skill_id) if skill_id else None
    bindings = suggest_bindings(ctx, skill_id) if skill_id else {}
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
    # review item 7).
    try:
        ctx.persistence.find_runs(["queued"])
        db_ok = True
        detail = None
    except Exception:
        db_ok = False
        detail = "database connectivity check failed"
        _LOG.exception("service.ready(): database connectivity check failed")
    return {"ready": db_ok, "backend": ctx.backend, "detail": detail}
