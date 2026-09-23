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

`ORCH_BACKEND=local` (env) selects LocalPersistence + a local-file
DataSourceAdapter for local/E2E runs (against synthetic_data/ by default) --
surfaced everywhere as data_mode "Local test data". Anything else selects
DeltaPersistence + UCTableDataSource (orchestrator/adapters/datasource_uc.py,
owned by the Unity Catalog work) against Unity Catalog, surfaced as
"Unity Catalog".
"""

from __future__ import annotations

import json
import os
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
from orchestrator.nodes.context import NodeContext
from orchestrator.nodes.fieldwork import NODES_FOR
from orchestrator.pipeline import NODE_STAGE_LABELS
from orchestrator.skill_registry import register_skill
from orchestrator.skills import load_skill
from orchestrator.state import RunState, to_json
from orchestrator.timeutil import utc_now

REPO_ROOT = Path(__file__).resolve().parent.parent

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
    data_source_factory: Callable[[dict[str, str]], Any]
    export_storage: Any
    clock: Callable[[], str]
    executor: Any = None
    # Not in the UI-facing signature list above, but needed to disambiguate
    # local-vs-UC behaviour (data_mode label, list_governed_tables/
    # suggest_bindings) without the UI ever branching on it itself.
    backend: str = "uc"
    local_data_root: Path | None = None


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

    def factory(bindings: dict[str, str]):
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


def _compute_run_fingerprint(ctx: AppContext, skill_dir: Path, source_table_versions: dict[str, str]) -> dict:
    reference_dir = skill_dir / "reference"
    reference_files = (
        sorted(p for p in reference_dir.rglob("*") if p.is_file()) if reference_dir.is_dir() else []
    )
    return compute_fingerprint(
        settings=ctx.settings,
        source_table_versions=source_table_versions,
        uploaded_file_hashes={},
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
    data_source = ctx.data_source_factory(bindings)
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
    return _compute_run_fingerprint(ctx, skill_dir, pinned_versions)


def build_app_context(env: dict | None = None) -> AppContext:
    env = os.environ if env is None else env
    settings = load_settings(env)
    backend = (env.get("ORCH_BACKEND") or "").strip().lower()
    skills_dir = Path(env.get("SKILLS_DIR") or (REPO_ROOT / "skills"))
    clock = utc_now

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
            clock=clock, backend="local", local_data_root=data_root,
        )
    else:
        persistence = DeltaPersistence(settings)
        persistence.migrate()

        def _uc_factory(bindings: dict[str, str], _settings=settings):
            try:
                from orchestrator.adapters.datasource_uc import UCTableDataSource
            except ImportError as exc:  # pragma: no cover - depends on a sibling agent's file
                from orchestrator.errors import ConfigError

                raise ConfigError(
                    "orchestrator.adapters.datasource_uc.UCTableDataSource is not available "
                    "yet -- the Unity Catalog source-data work has not landed in this checkout"
                ) from exc
            return UCTableDataSource(_settings, bindings)

        export_storage = VolumeExportStorage(volume_root=settings.volume) if settings.volume else None
        ctx = AppContext(
            settings=settings, persistence=persistence, skills_dir=skills_dir,
            data_source_factory=_uc_factory, export_storage=export_storage,
            clock=clock, backend="uc",
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


def _plan_test_entries(plan_tests: list[dict]) -> list[dict]:
    """Flattens plan.yaml's `tests` into {test_id, flag, primitive} rows the
    UI can join against flagged_rows.flag -- one row per RF_* flag a plan
    test declares, including a not_testable test's (plural) declared flags,
    each with primitive=None since none ran."""
    entries: list[dict] = []
    for t in plan_tests:
        test_id = t["test_id"]
        if "not_testable" in t:
            for flag in t["not_testable"].get("flags", []):
                entries.append({"test_id": test_id, "flag": flag, "primitive": None})
        elif t.get("flag"):
            entries.append({"test_id": test_id, "flag": t["flag"], "primitive": t.get("primitive")})
    return entries


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
        {"source": name, "columns": list((cfg or {}).get("columns", {}).keys())}
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
    tests_with_plan = [
        {**t, "plan_tests": plan_tests_by_catalogue.get(t.get("test_id"), [])} for t in tests
    ]
    flag_to_test = {e["flag"]: e["test_id"] for e in plan_entries}

    return {**base, "sources": sources, "tests": tests_with_plan, "flag_to_test": flag_to_test}


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
            out.append(
                {
                    "fqn": file_name,
                    "catalog": None,
                    "schema": None,
                    "table": name,
                    "comment": "Local test data" if (Path(root) / file_name).is_file() else "File not found",
                    "columns": list((cfg or {}).get("columns", {}).keys()),
                }
            )
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
) -> str:
    skill_dir = _skill_dir_for(ctx, skill_id)
    skill = load_skill(skill_dir)
    skill.validate()

    contract_sources = skill.contract.get("sources", {})
    missing = sorted(set(contract_sources) - set(bindings))
    if missing:
        raise ContractViolation([f"no binding supplied for contract source {s!r}" for s in missing])

    data_source = ctx.data_source_factory(bindings)

    # Resolve every source's version FIRST, before any read (CLAUDE.md §4.1
    # TOCTOU ordering) -- these become both this run's pinned data_assets
    # bindings and the fingerprint's source_table_versions.
    source_versions = {name: data_source.resolve_version(name) for name in contract_sources}

    now = ctx.clock()
    fingerprint = _compute_run_fingerprint(ctx, skill_dir, source_versions)

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

    options = {"auto_confirm_plan": not review_plan_first}
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
        options=options,
        data_assets=data_assets,
        fingerprint=fingerprint,
        now=now,
    )

    if ctx.executor is not None:
        ctx.executor.start(state.run_id, state.phase)

    return state.run_id


def get_run(ctx: AppContext, run_id: str) -> dict:
    state = ctx.persistence.load_state(run_id)
    payload = json.loads(to_json(state))
    payload["status_label"] = state.status.replace("_", " ").title()

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

        out.append(
            {
                "run_id": run_id,
                "skill_id": r.get("skill_id"),
                "skill_name": skill_entry["name"] if skill_entry else r.get("skill_id"),
                "audit_period": f"{r['audit_period_start']} – {r['audit_period_end']}",
                "run_timestamp": r["created_at"],
                "run_owner": r["run_owner"],
                "data_mode": data_mode,
                "status": str(r["status"]).replace("_", " ").title(),
                "findings_count": len(findings),
                "high_risk_count": sum(1 for f in findings if f["severity"] == "High"),
                "potential_exposure": exposure_metric["value"] if exposure_metric else 0,
                "open_actions": len(open_actions),
                "last_updated": r["last_state_change_at"],
                "has_workspace": bool(skill_entry and skill_entry.get("has_workspace")),
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

    return {
        "run_id": run_id,
        "status": state.status,
        "audit_period": f"{state.audit_period[0]} to {state.audit_period[1]}",
        "metrics": metrics_payload,
        "test_results": state.test_results,
        "not_testable": not_testable,
        "findings": findings,
        "reconciliation": state.reconciliation,
        "exposure": {
            "headline": exposure_metric["value"] if exposure_metric else None,
            "basis": (exposure_metric.get("source_ref") or {}).get("basis") if exposure_metric else None,
        },
    }


def _not_testable_flags(skill) -> set[str]:
    flags: set[str] = set()
    for t in skill.plan.get("tests", []):
        if "not_testable" in t:
            flags.update(t["not_testable"].get("flags", []))
    return flags


def get_run_frames(ctx: AppContext, run_id: str) -> dict[str, pd.DataFrame]:
    """Row-level frames for /workspace/tne: one DataFrame per contract source
    this run bound, holding that source's own contract-typed columns
    (__source, __row_key join keys included) plus one 0/1 int column per RF_*
    flag this run produced against that source -- <NA> (nullable Int64) for a
    not_testable test's declared flag, never a guessed 0. See FRAME_COLUMNS
    (populated per Skill on first call) for the exact column list.

    Built from the bound sources at the PINNED versions recorded in
    state.data_assets, plus flagged_rows pivoted into RF_* columns -- never
    from a live re-resolve, and never a placeholder default for a missing
    value (CLAUDE.md NN14)."""
    state = ctx.persistence.load_state(run_id)
    skill_dir = _skill_dir_for(ctx, state.skill_id)
    skill = load_skill(skill_dir)

    bindings = {b["source"]: b["table_fqn"] for b in state.data_assets}
    versions = {b["source"]: b["version"] for b in state.data_assets}
    data_source = ctx.data_source_factory(bindings)

    flagged_rows = ctx.persistence.list_flagged_rows(run_id)
    by_source: dict[str, list[dict]] = {}
    for r in flagged_rows:
        by_source.setdefault(r["source"], []).append(r)

    not_testable_flags = _not_testable_flags(skill)

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
        for flag in not_testable_flags:
            if flag not in df.columns:
                df[flag] = pd.array([pd.NA] * len(df), dtype="Int64")

        frames[source] = df
        FRAME_COLUMNS[source] = tuple(df.columns)

    return frames


def get_export(ctx: AppContext, run_id: str, kind: str) -> tuple[str, bytes]:
    exports = ctx.persistence.list_exports(run_id)
    match = next((e for e in exports if e["kind"] == kind), None)
    if match is None:
        raise FileNotFoundError(f"no {kind!r} export recorded for run {run_id!r}")
    content = ctx.export_storage.read(match["path"])
    filename = Path(match["path"]).name
    return filename, content


# ── health ────────────────────────────────────────────────────────────────────


def health(ctx: AppContext) -> dict:
    return {"status": "ok"}


def ready(ctx: AppContext) -> dict:
    try:
        ctx.persistence.find_runs(["queued"])
        db_ok = True
        detail = None
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the caller
        db_ok = False
        detail = repr(exc)
    return {"ready": db_ok, "backend": ctx.backend, "detail": detail}
