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
    # find_synthesis/find_candidates (P6 WP N5, docs/specs/P6_narration_design.md
    # §4.1): the narrate node's remaining two Sonnet-routed tasks -- additive
    # only, alongside the six narration task keys already present above/below.
    "find_synthesis": "model_sonnet",
    "find_candidates": "model_sonnet",
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
    # DeltaPersistence's bounded connection pool (P3/P4 perf gap review
    # 2026-09-25, bounded-pool follow-up): Werkzeug's threaded dev server
    # (app.yaml's `threaded=True`) spawns a new OS thread per HTTP request, so
    # a per-thread connection would open a fresh Databricks SQL session
    # (~1s+, session churn) on every render/poll, unbounded under concurrent
    # load. A pool of at most this many connections, reused across requests
    # and executor threads, bounds how many sessions this process ever holds
    # against the warehouse. Operational knob, not computation -- excluded
    # from the runtime config hash below, same as max_concurrent_runs.
    max_connections: int = 6
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
    # Perf review 2026-09-25 (found live: a 4-way concurrent narration run
    # exhausted a single retry against the workspace's QPS limit and wrongly
    # tripped the circuit breaker for sibling items). Operational knobs --
    # excluded from the runtime config hash below, same reasoning as
    # llm_retry_backoff_s: they change how a call is retried, never what a
    # node computes.
    #
    # Round-4 narration-content fix (2026-09-25, task item 2): the previous
    # defaults (3 attempts, 30s cap) gave a worst-case total backoff of only
    # 5+10=15s across the two waits between three attempts -- nowhere near
    # the ~60s a Databricks Model Serving workspace's PER-MINUTE rate limit
    # (REQUEST_LIMIT_EXCEEDED "output tokens per minute"/"QPS") needs to
    # reset, so a genuinely transient per-minute exhaustion looked identical
    # to a longer outage and exhausted retries before the limit could ever
    # clear (observed live: prioritise/act/find_synthesis batch calls fell
    # back to fallback_unavailable on exactly this shape of 429). 5 attempts
    # with a 75s cap gives 4 waits -- min(5,75)=5, min(10,75)=10,
    # min(20,75)=20, min(40,75)=40 -- a worst-case backoff of 5+10+20+40=75s
    # before the 5th (final) attempt, plus up to 25% jitter on each wait
    # (orchestrator.llm.gateway._call_live), so up to ~94s of sleep for one
    # call that is rate-limited on every attempt; a run's ~45 short
    # narration calls run narration_max_parallel (default 2) at a time, so a
    # run where every single call is persistently rate-limited for its full
    # retry budget adds, in the true worst case, roughly
    # ceil(45/2) * 94s =~ 35 minutes -- a theoretical ceiling from assuming
    # sustained saturation on every call, not what a real transient burst
    # costs (most calls succeed on the first or second attempt; this bound
    # exists to be stated, not to be a realistic expectation). A GENUINELY
    # DEAD endpoint (disabled, 403, no such endpoint) never enters this
    # backoff loop at all: `ModelUnavailable(permanent=True)`
    # (orchestrator/llm/errors.py) is raised and returned immediately by
    # `_call_live`'s own `except ModelUnavailable` branch, with no retry and
    # no sleep, regardless of these two settings -- see
    # tests/test_llm_gateway.py's permanent-unavailability tests and
    # tests/test_config.py's coverage of these two defaults.
    llm_max_transport_attempts: int = 5
    llm_retry_backoff_max_s: float = 75.0
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
    # docs/specs/P6_P8_explorer_llm_design.md §3.2 (Explorer Mode / LLM layer
    # config). All of these ARE part of the runtime config hash (below) --
    # unlike the operational knobs above, each one changes what a run
    # actually computes or what a prompt actually contains.
    llm_cache_mode: str = "live"  # "live" | "replay" -- orchestrator.llm.gateway.LLMGateway (WP N6)
    # Explorer refuses to start with this unset (CLAUDE.md NN14) -- there is
    # no reasonable default for an audit-period timezone.
    audit_timezone: str | None = None
    # docs/specs/P6_narration_design.md §8 (WP N6 additive keys). Off by
    # default everywhere except the dev `.env`, which sets both true
    # (§8's own default proposal) -- neither switch has a code-level
    # default other than false, so an unconfigured deployment never
    # narrates or proposes findings by accident.
    narration_enabled: bool = False
    ai_proposed_findings_enabled: bool = False
    # §5.1: the `find_candidates` task's schema caps `candidates` at this
    # many items -- a hard ceiling on how many AI-proposed findings one run
    # can produce, never just a UI truncation.
    narration_max_candidates: int = 3
    # Found-live perf review 2026-09-25: `narrate` was making its ~45+
    # narration calls strictly sequentially, so a full SKILL-001 run against
    # a reasoning model took 9+ minutes of an auditor's wait before
    # sign-off, even though most of those calls have no data dependency on
    # each other (orchestrator.nodes.narration.narrate runs its independent
    # stage -- profile, one call per finding, synthesis, priority,
    # remediation, candidates, captions -- through a bounded
    # ThreadPoolExecutor of this size; exec_summary alone waits for
    # synthesis's themes, so it always runs after). An operational knob --
    # it changes how fast a run narrates, never what it computes or stores
    # (results are still assembled in the SAME deterministic order
    # regardless of completion order) -- so it is excluded from the runtime
    # config hash below, same as the executor/admission knobs. 1 recovers
    # the old fully-sequential behaviour.
    #
    # Default is 2, NOT 4 (re-measured live 2026-09-25, same method): this
    # development workspace's shared MODEL_SONNET/MODEL_GPT_OSS endpoint has
    # a low per-workspace QPS/output-tokens-per-minute ceiling (CLAUDE.md
    # §6's own recorded override -- both roles point at the same
    # `databricks-gpt-oss-120b` here). At 4, even with bounded retry
    # (`llm_max_transport_attempts`) most single-call narration tasks and
    # one finding still exhausted their retries against SUSTAINED
    # contention -- not a blip a backoff can ride out, but four long
    # completions steadily eating the same per-minute budget -- landing 28
    # of the run's narratives on `fallback_unavailable`. At 2, only one task
    # (`find_synthesis`, a single edge case) still did; every one of the
    # 11 findings, and every other single-call task, completed as real
    # model output. This is a property of THIS shared, rate-limited
    # endpoint, not of the mechanism -- re-measure before raising it in a
    # workspace with a dedicated or higher-throughput endpoint.
    narration_max_parallel: int = 2
    # Explicit override of which repo Skills the planner sees as reference
    # examples (§4.4); empty means "the first two valid repo Skills sorted by
    # id" -- a later step's concern to resolve, this field only carries an
    # explicit override when one is set.
    explorer_reference_skill_ids: tuple[str, ...] = ()
    explorer_category_max_distinct: int = 30
    explorer_category_min_count: int = 5
    explorer_max_columns: int = 200
    explorer_max_prompt_chars: int = 240_000
    # UC column-tag names that mark a column PII (§4.3.1 rule 2). Empty means
    # no tag name is treated as a PII marker -- classification then falls
    # through to the contract-flag and heuristic rules only.
    pii_tag_names: tuple[str, ...] = ()
    # Independent review round 2 (BUG-EXPLORER-1, 2026-09-25): UCTableDataSource.
    # list_tables()'s default (catalog=None) enumeration walks every catalog and
    # schema this identity can read, with no notion of "which of these are
    # actual audit source data" -- so the Explorer source checklist and the
    # Playbook data-search box both surfaced this platform's OWN operational
    # tables (the ledger schema, and other internal/app schemas) ahead of real
    # business source tables. `DBX_SOURCE_SCHEMAS` (already read by
    # scripts/deploy_app.py for grants -- CLAUDE.md §7) doubles as an ALLOW-list
    # here when set: comma-separated `catalog.schema` names, the only schemas
    # list_tables()'s default enumeration walks at all. Empty means no
    # allow-list narrowing (falls back to walking every catalog/schema this
    # identity can read, minus the exclusions below) -- never a silent
    # narrowing nobody configured.
    # P7 review workflow (docs/specs/P7_mapping_authoring_design.md §3.6):
    # code default is "enforced" -- there is no environment where sign-off is
    # silently unguarded unless a deployment explicitly opts into "labelled"
    # (the dev-workspace override, D-P7-7). "labelled" still runs every role
    # check; it only waives the distinct-person rule and labels the result.
    review_sod_mode: str = "enforced"
    review_role_source: str = "workspace_groups"
    # Comma-separated exact workspace group display names (D-P7-6). Empty
    # (the code default) means "not configured" -- orchestrator.identity's
    # build_role_resolver refuses to build a workspace_groups resolver in
    # enforced mode with any of these unset (ConfigError), rather than
    # silently admitting every identity to every role.
    review_preparer_groups: tuple[str, ...] = ()
    review_reviewer_groups: tuple[str, ...] = ()
    review_approver_groups: tuple[str, ...] = ()
    # Gitignored YAML path, {email: [role, ...]} -- required iff
    # review_role_source == "config" (orchestrator.identity.build_role_resolver
    # raises ConfigError otherwise). Local backend / e2e tests only (§3.5).
    review_role_assignments_path: str | None = None
    source_schemas: tuple[str, ...] = ()
    # A configurable exclusion list, same `catalog.schema` shape, for any
    # other internal/app schema that should never appear in source discovery
    # -- in ADDITION to the ledger's own catalog.schema (DBX_CATALOG.DBX_SCHEMA),
    # which is always excluded regardless of this list (never a hardcoded
    # schema name -- CLAUDE.md NN16).
    excluded_schemas: tuple[str, ...] = ()
    # LIFECYCLE_design.md §2.6: required to start any lifecycle run_kind
    # that calls a model at all (fieldwork never checks this -- `execute`
    # never calls an LLM, CLAUDE.md non-negotiable 2). Unset means no
    # lifecycle run that calls a model may start; there is no silent
    # unlimited default. An operational spending cap, not computation --
    # excluded from the runtime config hash below, same as the executor/
    # admission knobs.
    llm_monthly_token_budget: int | None = None
    # A JSON blob of {role: price_per_million_tokens}, read only by whatever
    # later displays an estimated cost ("estimate at configured rates").
    # Unset means no price is shown anywhere -- never a fabricated number
    # (CLAUDE.md non-negotiable 13). No prices appear in code.
    llm_price_per_mtok_json: str | None = None

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


def _parse_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(v.strip() for v in value.split(",") if v.strip())
def _parse_optional_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


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
        max_connections=_parse_int(env.get("DBX_MAX_CONNECTIONS"), 6),
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
        llm_max_transport_attempts=_parse_int(env.get("LLM_MAX_TRANSPORT_ATTEMPTS"), 5),
        llm_retry_backoff_max_s=_parse_float(env.get("LLM_RETRY_BACKOFF_MAX_S"), 75.0),
        enable_row_level_llm=_parse_bool(env.get("ENABLE_ROW_LEVEL_LLM"), False),
        apps_python_version=env.get("DBX_APPS_PYTHON_VERSION") or "3.11",
        llm_monthly_token_budget=_parse_optional_int(env.get("LLM_MONTHLY_TOKEN_BUDGET")),
        llm_price_per_mtok_json=env.get("LLM_PRICE_PER_MTOK_JSON") or None,
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
        llm_cache_mode=env.get("LLM_CACHE_MODE") or "live",
        audit_timezone=env.get("AUDIT_TIMEZONE") or None,
        narration_enabled=_parse_bool(env.get("NARRATION_ENABLED"), False),
        ai_proposed_findings_enabled=_parse_bool(env.get("AI_PROPOSED_FINDINGS_ENABLED"), False),
        narration_max_candidates=_parse_int(env.get("NARRATION_MAX_CANDIDATES"), 3),
        narration_max_parallel=_parse_int(env.get("NARRATION_MAX_PARALLEL"), 2),
        explorer_reference_skill_ids=_parse_csv(env.get("EXPLORER_REFERENCE_SKILL_IDS")),
        explorer_category_max_distinct=_parse_int(env.get("EXPLORER_CATEGORY_MAX_DISTINCT"), 30),
        explorer_category_min_count=_parse_int(env.get("EXPLORER_CATEGORY_MIN_COUNT"), 5),
        explorer_max_columns=_parse_int(env.get("EXPLORER_MAX_COLUMNS"), 200),
        explorer_max_prompt_chars=_parse_int(env.get("EXPLORER_MAX_PROMPT_CHARS"), 240_000),
        pii_tag_names=_parse_csv(env.get("PII_TAG_NAMES")),
        source_schemas=_parse_csv(env.get("DBX_SOURCE_SCHEMAS")),
        excluded_schemas=_parse_csv(env.get("DBX_EXCLUDED_SCHEMAS")),
        review_sod_mode=env.get("REVIEW_SOD_MODE") or "enforced",
        review_role_source=env.get("REVIEW_ROLE_SOURCE") or "workspace_groups",
        review_preparer_groups=_parse_csv(env.get("REVIEW_PREPARER_GROUPS")),
        review_reviewer_groups=_parse_csv(env.get("REVIEW_REVIEWER_GROUPS")),
        review_approver_groups=_parse_csv(env.get("REVIEW_APPROVER_GROUPS")),
        review_role_assignments_path=env.get("REVIEW_ROLE_ASSIGNMENTS") or None,
    )


# Operational knobs excluded from the runtime config hash: they change how a run is
# scheduled (concurrency cap, which Executor runs it), never what it computes, so two
# runs configured identically except for these should share a fingerprint (CLAUDE.md
# §4.1, non-blocking item).
_RUNTIME_HASH_EXCLUDED_FIELDS = frozenset({
    "max_concurrent_runs", "max_connections", "executor", "mlflow_tracking_uri", "mlflow_experiment_path",
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
    "llm_max_transport_attempts",
    "llm_retry_backoff_max_s",
    "apps_python_version",
    "llm_monthly_token_budget",
    "llm_price_per_mtok_json",
    "narration_max_parallel",
    # Source-discovery filtering only (which tables the UI offers to pick
    # from) -- never what a bound run reads or computes, so two runs
    # configured identically except for these should still share a
    # fingerprint (same reasoning as source_bindings_path above).
    "source_schemas",
    "excluded_schemas",
    # P7 review workflow (§3.6): the review policy never changes a number or
    # a finding, only who may act and when -- hashing it would make
    # verify_fingerprint (run at every executor pass) refuse to export a
    # paused run after a policy/group-name change with no effect on what was
    # computed. The policy actually in force is instead recorded on every
    # review_steps row and in RunState.signoff.
    "review_sod_mode",
    "review_role_source",
    "review_preparer_groups",
    "review_reviewer_groups",
    "review_approver_groups",
    "review_role_assignments_path",
})


def runtime_config_hash(settings: Settings) -> str:
    payload_dict = {
        k: v for k, v in asdict(settings).items() if k not in _RUNTIME_HASH_EXCLUDED_FIELDS
    }
    payload = json.dumps(payload_dict, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def endpoint_config(settings: Settings) -> dict[str, str | None]:
    return {task: getattr(settings, attr) for task, attr in NODE_MODELS.items()}
