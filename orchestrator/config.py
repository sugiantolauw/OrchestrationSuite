from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field

from orchestrator.errors import ConfigError

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

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
})


def runtime_config_hash(settings: Settings) -> str:
    payload_dict = {
        k: v for k, v in asdict(settings).items() if k not in _RUNTIME_HASH_EXCLUDED_FIELDS
    }
    payload = json.dumps(payload_dict, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def endpoint_config(settings: Settings) -> dict[str, str | None]:
    return {task: getattr(settings, attr) for task, attr in NODE_MODELS.items()}
