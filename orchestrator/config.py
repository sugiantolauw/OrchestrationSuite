from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from orchestrator.errors import ConfigError

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# CLAUDE.md §4.7: the PPTX exporter's own content-free template (layouts +
# masters, zero content slides -- scripts/strip_pptx_template.py generates
# it from the source deck). A relative default resolves against the repo
# root so a checkout works with no .env at all; PPTX_TEMPLATE_PATH overrides
# it for a deployment layout where the repo root differs (CLAUDE.md §3
# non-negotiable 16 -- never a hardcoded absolute path).
_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PPTX_TEMPLATE_PATH = str(_REPO_ROOT / "templates" / "report_template.pptx")

# task -> Settings attribute name holding the endpoint for that task (CLAUDE.md §6)
NODE_MODELS: dict[str, str] = {
    "plan_explorer": "model_sonnet",
    "find": "model_sonnet",
    "export_summary": "model_sonnet",
    "profile": "model_sonnet",
    "prioritise": "model_sonnet",
    "act": "model_sonnet",
    "judge_quality": "model_sonnet",
    "classify": "model_gpt_oss",
    "export_caption": "model_gpt_oss",
    "plan_repair": "model_gpt_oss",
    "judge_grounding": "model_gpt_oss",
}


def _validate_identifier(name: str, value: str | None) -> None:
    if value is not None and not _IDENTIFIER_RE.match(value):
        raise ConfigError(f"{name}={value!r} is not a valid identifier")


@dataclass(frozen=True)
class Settings:
    catalog: str | None = None
    schema: str | None = None
    volume: str | None = None
    warehouse_http_path: str | None = None
    host: str | None = None
    app_name: str | None = None
    model_sonnet: str | None = None
    model_gpt_oss: str | None = None
    executor: str = "thread"
    max_concurrent_runs: int = 2
    demo_mode: bool = False
    code_revision: str | None = None
    mlflow_tracking_uri: str | None = None
    mlflow_experiment_path: str | None = None
    # P3 executor robustness (CLAUDE.md §2.3 rule 3 / §9C): bounded admission
    # retries with backoff, so a queued run whose lease repeatedly cannot be
    # acquired does not retry forever -- it exhausts and ends `failed` through
    # the normal state machine instead. Operational knobs, not computation --
    # excluded from the runtime config hash below, same as max_concurrent_runs.
    admission_max_attempts: int = 20
    admission_backoff_base_s: float = 2.0
    admission_backoff_max_s: float = 60.0
    # Executor admission-loop cadence (found-live cost review: the loop was
    # polling the warehouse every 2s unconditionally, ~3,000 queries/hour with
    # nothing queued or running; a follow-up review found even a 10-minute
    # idle sweep still woke a 1-minute-auto-stop warehouse ~6x/hour, a
    # 15-20% duty cycle worth tens of dollars/day for a single-container
    # deployment where every real transition already wakes the executor
    # directly). "Active" applies while this worker has any run in flight or
    # is backing off a lease retry. "Idle" is a periodic safety sweep for the
    # rest of the time -- 0 (the default) disables it entirely: the loop
    # still reaps orphans and admits any already-queued run once, immediately,
    # on every start() (so nothing is missed at App start), and otherwise
    # blocks until an in-process signal wakes it (start_audit_run/
    # confirm_plan/sign_off/resume_run all call executor.start(run_id, phase)
    # directly, which admits that run itself and also nudges the loop).
    # A single-container deployment never needs this sweep for anything else.
    # Set EXECUTOR_IDLE_POLL_INTERVAL_S to a positive number ONLY for a
    # multi-container deployment, where a run admitted by one container must
    # still be noticed (queued/orphaned) by another that never itself
    # received a direct wake for it. Operational knobs, not computation --
    # excluded from the runtime config hash, same as the admission-backoff
    # fields above.
    executor_active_poll_interval_s: float = 30.0
    executor_idle_poll_interval_s: float = 0.0
    pptx_template_path: str = DEFAULT_PPTX_TEMPLATE_PATH
    # Independent review 2026-09-24 item 1: per-environment source-binding
    # config (orchestrator.source_bindings) -- an exact, pre-declared
    # mapping of a Skill's contract sources to a Volume file or a governed
    # UC table, for a workspace whose sources are Excel files in a Volume
    # rather than Delta tables. A gitignored path, never a repo-committed
    # one (CLAUDE.md NN16); unset means no configured bindings at all.
    # Excluded from the runtime config hash below, same reasoning as
    # pptx_template_path: the run's OWN pinned source_table_versions/
    # uploaded_file_hashes already record exactly what was read, so two
    # runs against identical data should share a fingerprint regardless of
    # which config file pointed at it.
    source_bindings_path: str | None = None
    # Readiness caching (independent review item 5): results are cached for
    # this many seconds so /ready does not re-probe the Volume/warehouse/
    # model endpoints on every poll (cost -- CLAUDE.md §11 cost incident).
    readiness_cache_ttl_s: float = 120.0
    # LLM layer (independent review item 3; docs/specs/P6_P8_explorer_llm_
    # design.md §3.2). Operational knobs -- excluded from the runtime config
    # hash below, same as the executor/admission ones: they change how a
    # call is retried, never what a node computes.
    llm_timeout_s: float = 180.0
    llm_retry_backoff_s: float = 5.0
    # Independent review item 4: T4.3 row-level classification via the
    # model client is BUILT but switched off by default -- while off, T4.3
    # stays `not_testable` ("awaiting governance approval to send expense
    # descriptions to a model"), per the corporate-workspace decision
    # (CLAUDE.md §11). This one IS part of the runtime config hash: it
    # changes what a run actually computes for T4.3.
    enable_row_level_llm: bool = False
    # Independent review item 6: the Python version Databricks Apps run on
    # -- 3.11 as of this writing -- used by scripts/build_vendor_wheelhouse.py
    # to select the right wheel for each binary dependency. A config value
    # (CLAUDE.md NN16), never hardcoded in the vendoring script itself.
    apps_python_version: str = "3.11"

    def __post_init__(self) -> None:
        _validate_identifier("catalog", self.catalog)
        _validate_identifier("schema", self.schema)

    def require(self, *names: str) -> None:
        missing = [n for n in names if getattr(self, n, None) in (None, "")]
        if missing:
            raise ConfigError(f"missing required configuration: {', '.join(missing)}")


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _parse_int(value: str | None, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _parse_float(value: str | None, default: float) -> float:
    if value is None or value == "":
        return default
    return float(value)


def load_settings(env: dict | None = None) -> Settings:
    env = os.environ if env is None else env
    return Settings(
        catalog=env.get("DBX_CATALOG") or None,
        schema=env.get("DBX_SCHEMA") or None,
        volume=env.get("DBX_VOLUME") or None,
        warehouse_http_path=env.get("DBX_WAREHOUSE_HTTP_PATH") or None,
        host=env.get("DATABRICKS_HOST") or None,
        app_name=env.get("DBX_APP_NAME") or None,
        model_sonnet=env.get("MODEL_SONNET") or None,
        model_gpt_oss=env.get("MODEL_GPT_OSS") or None,
        executor=env.get("EXECUTOR") or "thread",
        max_concurrent_runs=_parse_int(env.get("MAX_CONCURRENT_RUNS"), 2),
        demo_mode=_parse_bool(env.get("DEMO_MODE"), False),
        code_revision=env.get("CODE_REVISION") or None,
        admission_max_attempts=_parse_int(env.get("ADMISSION_MAX_ATTEMPTS"), 20),
        admission_backoff_base_s=_parse_float(env.get("ADMISSION_BACKOFF_BASE_S"), 2.0),
        admission_backoff_max_s=_parse_float(env.get("ADMISSION_BACKOFF_MAX_S"), 60.0),
        executor_active_poll_interval_s=_parse_float(env.get("EXECUTOR_ACTIVE_POLL_INTERVAL_S"), 30.0),
        executor_idle_poll_interval_s=_parse_float(env.get("EXECUTOR_IDLE_POLL_INTERVAL_S"), 0.0),
        pptx_template_path=env.get("PPTX_TEMPLATE_PATH") or DEFAULT_PPTX_TEMPLATE_PATH,
        source_bindings_path=env.get("SOURCE_BINDINGS") or None,
        readiness_cache_ttl_s=_parse_float(env.get("READINESS_CACHE_TTL_S"), 120.0),
        llm_timeout_s=_parse_float(env.get("LLM_TIMEOUT_S"), 180.0),
        llm_retry_backoff_s=_parse_float(env.get("LLM_RETRY_BACKOFF_S"), 5.0),
        enable_row_level_llm=_parse_bool(env.get("ENABLE_ROW_LEVEL_LLM"), False),
        apps_python_version=env.get("DBX_APPS_PYTHON_VERSION") or "3.11",
        # P2/P3 gate review item 4 (MLflow per-node spans, CLAUDE.md §2.3).
        # Unset -- never hardcoded here -- means mlflow's own default
        # resolution: MLFLOW_TRACKING_URI if the process environment already
        # sets it (mlflow reads that itself), else a local `./mlruns` file
        # store. "databricks" (the managed-tracking URI scheme, not a
        # workspace name) is a valid value for this same variable, set
        # through the environment like every other workspace-specific value
        # (CLAUDE.md §7/non-negotiable 16).
        mlflow_tracking_uri=env.get("MLFLOW_TRACKING_URI") or None,
        # MLflow experiment (CLAUDE.md P2/P3 gate review, MLflow-on-the-
        # platform item): Databricks-managed tracking (MLFLOW_TRACKING_URI=
        # databricks) requires the experiment name to be an ABSOLUTE
        # workspace path (e.g. "/Shared/<app>-audit-runs") -- the adapter's
        # own hardcoded default ("orchestrator-audit-runs") is not one. Unset
        # here means the adapter falls back to that generic default, which
        # is fine for a non-Databricks tracking URI (a local sqlite store in
        # tests) where path-ness does not matter; a real deployment always
        # sets this explicitly (scripts/deploy_app.py derives it from
        # DBX_APP_NAME, never hardcoded here -- CLAUDE.md §7/NN16).
        mlflow_experiment_path=env.get("MLFLOW_EXPERIMENT_PATH") or None,
    )


# Operational knobs excluded from the runtime config hash: they change how a run is
# scheduled (concurrency cap, which Executor runs it), never what it computes, so two
# runs configured identically except for these should share a fingerprint (CLAUDE.md
# §4.1, non-blocking item).
_RUNTIME_HASH_EXCLUDED_FIELDS = frozenset({
    "max_concurrent_runs", "executor", "mlflow_tracking_uri", "mlflow_experiment_path",
    "admission_max_attempts", "admission_backoff_base_s", "admission_backoff_max_s",
    "executor_active_poll_interval_s", "executor_idle_poll_interval_s",
    # PPTX template path changes the export's appearance only -- never a
    # number or a finding (CLAUDE.md §4.7's "every number comes from
    # RunState" rule), so two runs configured identically except for this
    # should still share a fingerprint.
    "pptx_template_path",
    "source_bindings_path",
    "readiness_cache_ttl_s",
    "llm_timeout_s",
    "llm_retry_backoff_s",
    "apps_python_version",
})


def runtime_config_hash(settings: Settings) -> str:
    payload_dict = {
        k: v for k, v in asdict(settings).items() if k not in _RUNTIME_HASH_EXCLUDED_FIELDS
    }
    payload = json.dumps(payload_dict, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def endpoint_config(settings: Settings) -> dict[str, str | None]:
    return {task: getattr(settings, attr) for task, attr in NODE_MODELS.items()}
