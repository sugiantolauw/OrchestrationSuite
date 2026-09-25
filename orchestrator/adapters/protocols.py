from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from orchestrator.state import RunState


class DataSourceAdapter(Protocol):
    """Gives the caller the tested population without exposing pandas vs Spark."""

    def resolve_source_versions(self, table_fqns: list[str]) -> dict[str, str]:
        ...

    def resolve_version(self, source: str) -> str:
        """Resolve a single source's version (e.g. via DESCRIBE HISTORY). Callers must
        resolve_version() first and pass the *same* version into read_population's
        VERSION AS OF -- resolving after the read would leave a TOCTOU gap between what
        was read and what provenance records were captured (CLAUDE.md §4.1)."""
        ...

    def read_population(
        self,
        source: str,
        *,
        version: int | str,
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

    def row_count(self, source: str, *, version: str | None = None) -> int:
        """Takes a bound source name, like resolve_version/read_population --
        not an already-qualified table_fqn. Used for G6's independent row count."""
        ...

    def column_stats(
        self,
        source: str,
        *,
        version: str | None = None,
        amount_column: str | None = None,
        date_column: str | None = None,
    ) -> dict:
        """Independently computed Sigma(amount)/min-max date for a bound source, at
        the same pinned version -- same pattern as row_count (a SEPARATE read/
        aggregation, never reused from the engine's own in-memory population).
        Used for G6's amount and date reconciliation (CLAUDE.md §5 G6, P2/P3
        gate review item 2). Either column may be omitted when a source has no
        single natural amount or date column; the corresponding key is then
        None. Returns {"amount": float | None, "min_date": str | None,
        "max_date": str | None} (dates as ISO 'YYYY-MM-DD')."""
        ...

    def profile_columns(
        self,
        source: str,
        *,
        version: int | str,
        max_distinct: int,
        min_count: int,
        audit_period: tuple[str, str] | None = None,
        audit_timezone: str | None = None,
    ) -> dict:
        """Explorer Mode's aggregates-only statistical profile of one source
        (docs/specs/P6_P8_explorer_llm_design.md §4.3): row_count and, per
        column, null_count/distinct_count/type/min/max/negative_count/
        zero_count/values (a low-cardinality column's value set with counts,
        capped at max_distinct and suppressing any value seen in fewer than
        min_count rows) and, for a date/datetime column, in_period_count when
        audit_period+audit_timezone are given. PII classification is NOT this
        method's job -- it returns raw statistics only; §4.3.1 masking is
        applied by the caller (orchestrator.explorer.profile), never here, so
        an adapter never has to know what a Skill or the heuristic rules
        consider PII. Never reads more than a bounded sample of distinct
        values per column (CLAUDE.md §2.3 rule 4 -- pushed into the warehouse
        for a UC-backed source; computed in pandas over the same population a
        Playbook profile() would read for a file-backed source)."""
        ...

    def distinct_count(self, source: str, *, version: int | str, columns: list[str]) -> int:
        """COUNT(DISTINCT <columns...>) at the pinned version -- used for
        Explorer's entry_key uniqueness check (§4.7 V-C3): a composite key is
        unique when this equals row_count(source, version=version)."""
        ...


@dataclass(frozen=True)
class ModelResponse:
    """A chat completion, already stripped of raw reasoning content (CLAUDE.md
    §3 non-negotiable 11 / NN11) -- independent review 2026-09-24 item 3,
    docs/specs/P6_P8_explorer_llm_design.md §3.5."""

    text: str                        # concatenated type=="text" parts; reasoning parts dropped
    served_model_version: str        # response.model, e.g. "gpt-oss-120b-080525"
    finish_reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    request_id: str | None           # x-request-id response header
    reasoning_parts_stripped: int    # count only -- never the text (NN11)
    latency_ms: int


class ModelClient(Protocol):
    """A single chat-completion call against one already-resolved endpoint
    name, with pre-filtered params (the caller -- orchestrator.llm.gateway.
    LLMGateway -- decides which parameters are safe to send for this role;
    this Protocol never guesses). Every error condition below is mapped to a
    typed exception from orchestrator.llm.errors, never an ad hoc dict or a
    bare requests/openai exception leaking through."""

    def chat(
        self, *, endpoint: str, messages: list[dict], params: dict, timeout_s: float,
    ) -> ModelResponse:
        ...

    def describe_endpoint(self, endpoint: str) -> dict:
        """A cheap metadata call (never a completion) -- {"foundation_model":
        ..., "ready": bool}. Used by readiness checks (independent review
        item 5)."""
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

    def get_fingerprints(self, fingerprint_ids: list[str]) -> dict[str, dict]:
        """Batched get_fingerprint (independent review 2026-09-24 item 6):
        {fingerprint_id: fingerprint}; a fingerprint_id that does not
        resolve is simply absent from the result (never a KeyError or a
        fabricated row) -- callers already treat "unknown fingerprint" as
        "nothing to say" (service._queue_affinity_note)."""
        ...

    def get_run_row(self, run_id: str) -> dict | None:
        """The raw `runs` projection row (never `run_state`) for one run --
        None if the run does not exist. CLAUDE.md §11 "Paused runs across a
        code deploy": the caller this exists for is get_run's
        `export_code_revision` lookup, a single-run read that does not
        warrant list_runs' join against run_state and its own status
        precedence rules."""
        ...

    def mark_run_superseded(self, run_id: str, *, superseded_by: str, now: str) -> None:
        """CLAUDE.md §11 "Paused runs across a code deploy" / independent
        review 2026-09-24 gap #11: orchestrator.service.restart_stale_run's
        "never deleted" half (CLAUDE.md §9A Q2, `runs.superseded_by`) -- the
        old run's own status/phase/RunState are left untouched (it stays
        exactly what it was refused at: `awaiting_confirmation`, `queued`,
        or `interrupted`), only `superseded_by` is set, the same
        already-established column `cross_run_totals` reads to exclude a
        superseded run from cross-run totals. Idempotent: setting it again
        to the SAME value is a no-op; the caller decides whether a run may
        be superseded more than once."""
        ...

    def record_export_code_revision(
        self, run_id: str, *, fingerprint_id: str, code_revision: str, now: str
    ) -> None:
        """CLAUDE.md §11 "Paused runs across a code deploy" / independent
        review 2026-09-24 gap #11: called only once verify_fingerprint has
        already accepted a code_revision difference for this run's export
        phase (orchestrator.pipeline.run_phase / orchestrator.runs.resume) --
        never a decision this method makes itself. run_fingerprints stays
        immutable (CLAUDE.md P1A), so this records the code revision that
        actually ran export in two additive places instead: `runs.
        export_code_revision` (a cheap direct read for get_run/list_runs/
        exports) and an append-only row in run_fingerprint_overrides (the
        fuller history -- run_id, phase, field, the fingerprint's original
        value, and the value actually used). Idempotent: a re-run of the
        export phase (or a second executor pass that finds the same
        override already recorded) writes the SAME override_id and does not
        duplicate the history row; `runs.export_code_revision` is simply
        kept at the latest recorded value."""
        ...

    def begin_node_attempt(
        self,
        *,
        run_id: str,
        phase: str,
        phase_epoch: int = 1,
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

    def repair_projections(self) -> int:
        """Rewrites any `runs` row whose state_version lags its `run_state` row (a save
        whose projection update failed and was swallowed, CLAUDE.md §9C/B4). Returns the
        number of rows repaired. Called at App start alongside the reaper."""
        ...

    def get_engagement(self, engagement_id: str) -> dict | None:
        ...

    def list_engagements(self) -> list[dict]:
        ...

    def record_skill_version(
        self,
        *,
        skill_id: str,
        version: str,
        content_hash: str,
        content: dict,
        created_by: str,
        now: str,
        status: str = "draft",
    ) -> dict:
        ...

    def get_skill_version(self, skill_id: str, version: str) -> dict | None:
        ...

    def list_skill_versions(self, skill_id: str) -> list[dict]:
        ...

    def list_skill_versions_by_origin(self, origin: str) -> list[dict]:
        """The latest row (by created_at) per skill_id whose content.origin ==
        `origin` -- app/'s /skills list use ('explorer_saved') so a saved
        Explorer draft appears there without a repo directory (docs/specs/
        P6_P8_explorer_llm_design.md §4.13 point 4). `origin` lives inside
        each row's content_json (record_skill_version's own `content` dict),
        not a separate indexed column, so this is necessarily a full scan --
        acceptable at the skill_versions table's scale (one row per Skill
        version, never per run)."""
        ...

    def list_all_skill_versions(self) -> list[dict]:
        """Every skill_versions row, ordered by skill_id then created_at
        ascending -- the SAME full-table scan list_skill_versions_by_origin
        already runs (see its own docstring: unavoidable at this table's
        scale), exposed once so a caller needing more than one view of the
        whole table (orchestrator.service.list_skills: per-skill last_updated
        AND the explorer_saved cards) reads it ONCE rather than once per
        view (P3/P4 perf gap review 2026-09-25, the /skills and /workspace/tne
        cold-load latency pass: list_skills used to call
        list_skill_versions(skill_id) once PER SKILL DIRECTORY plus
        list_skill_versions_by_origin -- N+2 round trips against the exact
        same table for what is, today, still one full scan's worth of data)."""
        ...

    def record_skill_surface2_results(self, *, skill_id: str, version: str, results_json: dict) -> None:
        """docs/specs/P7_mapping_authoring_design.md §2.2 "Recording Surface
        2 results": overwrites `skill_versions.surface2_results_json` for an
        existing (skill_id, version) row -- `orchestrator.service.
        record_skill_surface2`'s one write, never called from the pipeline.
        No-op silently if the row does not exist (the caller already checked
        via get_skill_version before calling this)."""
        ...

    def upsert_risks(self, risks: list[dict], *, now: str) -> None:
        ...

    def upsert_controls(self, controls: list[dict], *, now: str) -> None:
        ...

    def list_risks(self, engagement_id: str | None = None) -> list[dict]:
        ...

    def list_controls(self, engagement_id: str | None = None) -> list[dict]:
        ...

    def write_findings(
        self,
        run_id: str,
        findings: list[dict],
        *,
        engagement_id: str | None,
        skill_id: str | None,
        skill_version: str | None,
        now: str,
    ) -> list[dict]:
        ...

    def list_findings(self, run_id: str) -> list[dict]:
        ...

    def list_findings_for_runs(self, run_ids: list[str]) -> dict[str, list[dict]]:
        """Batched list_findings (independent review 2026-09-24 item 6): one
        query for every run_id in `run_ids` instead of one per run, so a
        run-listing page never pays an N+1 query cost. {run_id: [finding,
        ...]}; a run_id with no findings is present with an empty list, not
        omitted -- callers must never distinguish "no findings yet" from
        "never asked" by key absence."""
        ...

    def set_finding_review_state(self, finding_id: str, *, to_state: str, actor: str, now: str) -> dict:
        ...

    # ── P3 run outputs (CLAUDE.md §4.2, §9C) ────────────────────────────────

    def write_flagged_rows(self, run_id: str, rows: list[dict]) -> None:
        """Idempotent replace-per-run: `rows` (each {source, row_key, flag,
        group_id}) becomes this run's entire flagged_rows set -- a re-run of the
        node that produced them overwrites, never appends duplicates."""
        ...

    def list_flagged_rows(self, run_id: str, flag: str | None = None) -> list[dict]:
        ...

    def write_run_metrics(self, run_id: str, metrics: list[dict]) -> None:
        """Idempotent replace-per-run: `metrics` (each {metric_name, value,
        unit, source_ref, test_id}) becomes this run's entire run_metrics set."""
        ...

    def get_run_metrics(self, run_id: str) -> dict[str, dict]:
        ...

    def get_run_metrics_for_runs(self, run_ids: list[str]) -> dict[str, dict[str, dict]]:
        """Batched get_run_metrics (independent review 2026-09-24 item 6):
        {run_id: {metric_name: metric}}; a run_id with no metrics yet is
        present with an empty dict, never omitted."""
        ...

    def write_issues_for_findings(
        self, run_id: str, findings: list[dict], *, engagement_id: str | None, now: str
    ) -> list[dict]:
        """One issue per finding (issue_id = f'ISS-{finding_id}'), status 'draft'.
        Idempotent: a repeat call for an issue_id that already exists leaves it
        untouched (its status may have moved on since)."""
        ...

    def write_management_actions(self, run_id: str, actions: list[dict], *, now: str) -> None:
        """Idempotent replace-per-run, same shape as write_flagged_rows."""
        ...

    def list_management_actions(self, filters: dict | None = None) -> list[dict]:
        ...

    def list_management_actions_for_runs(self, run_ids: list[str]) -> dict[str, list[dict]]:
        """Batched list_management_actions (independent review 2026-09-24
        item 6), scoped to `run_id` only (unlike list_management_actions'
        general `filters`) -- {run_id: [action, ...]}, a run_id with none
        present with an empty list, never omitted."""
        ...

    def update_management_action(
        self, action_id: str, *, owner: str | None, status: str, target_date: str | None,
        response: str | None, updated_by: str, now: str,
    ) -> dict:
        """Independent review 2026-09-24 gap #3: persists an auditor's edit
        to an existing management action's owner/status/target_date and
        response (the `description` column -- the same field write_
        management_actions' own template-drafted text occupies; an edit
        here overwrites it, same as the prototype's tracker) -- never held
        only in a browser session. Raises ManagementActionNotFound if
        action_id names no row. Returns the updated row, same shape as
        list_management_actions' own rows."""
        ...

    def record_export(
        self, run_id: str, kind: str, *, path: str, sha256: str, created_by: str, now: str
    ) -> dict:
        ...

    def list_exports(self, run_id: str) -> list[dict]:
        ...

    # ── narration (P6, docs/specs/P6_narration_design.md §6.1 / WP N3b) ────

    def upsert_narrative(self, row: dict, *, expected_version: int | None = None) -> bool:
        """Idempotent by `narrative_id` (node rule 1, CLAUDE.md §2.3): `narratives`
        holds the CURRENT version per narrated field, keyed on `narrative_id =
        sha256(run_id|target_kind|target_id|field)[:32]` -- a re-execution of the
        node that produced this field's text overwrites the same row rather than
        appending. History (who/when/before/after) lives separately in
        `narrative_edits` (`append_narrative_edit`), never here.

        `expected_version` (P6 WP N10, §6.4's `edit_narrative` CAS): `None`
        (every narrate-node caller) is the existing unconditional upsert --
        insert if absent, otherwise overwrite regardless of the stored
        `version` -- and always returns `True`. A caller that passes it (only
        `edit_narrative`) asserts the row ALREADY EXISTS at exactly that
        version; the write applies, and this returns `True`, only if the
        stored row's `version` still equals `expected_version` at write time
        -- a conditional `UPDATE ... WHERE narrative_id = ? AND version = ?`,
        never a MERGE/upsert (there is nothing to insert: an edit's target
        narrative must already exist, `edit_narrative` already raises
        `NarrativeNotFound` otherwise). Zero rows affected (a racing edit or
        a `narrate()` re-execution landed first) returns `False` and writes
        nothing -- the caller turns that into `NarrativeEditConflict`, never
        a silent overwrite (CLAUDE.md NN14)."""
        ...

    def get_narratives(self, run_id: str) -> list[dict]:
        ...

    def append_narrative_edit(self, row: dict) -> None:
        """G14 append-only (CLAUDE.md §5 Tier B): inserts keyed on `edit_id =
        sha256(narrative_id|version)`, never updates or deletes an existing row --
        a retried write of the same edit_id is a no-op, not a duplicate or an
        overwrite."""
        ...

    def list_narrative_edits(self, run_id: str) -> list[dict]:
        """Not named in §6.1's contract table, added here as the obvious
        minimal completion of `append_narrative_edit` -- without a way to read
        it back, the append-only history it writes (G14, CLAUDE.md §5 Tier B)
        would be unreachable through this Protocol at all."""
        ...

    def write_candidates(self, run_id: str, candidates: list[dict], *, now: str) -> None:
        """Idempotent upsert by `candidate_id` (§5.1: `sha256(run_id|generation|
        rule_id)[:24]`) -- a re-executed `narrate` node for the SAME generation
        overwrites its own candidate rows, never appends duplicates. Unlike
        `write_flagged_rows`/`write_run_metrics`, this never prunes: a candidate
        already decided or superseded (§5.2) is never deleted, and a re-upsert
        never resets its decision fields (candidate_status, decided_by, decided_at,
        decision_reason, decided_severity, created_at are sticky, the same
        pattern `write_findings` uses for review-lifecycle columns)."""
        ...

    def list_candidates(self, run_id: str) -> list[dict]:
        ...

    def decide_candidate_cas(
        self,
        candidate_id: str,
        *,
        decision: str,
        reason: str | None,
        decided_severity: str | None,
        actor: str,
        now: str,
    ) -> bool:
        """A conditional `UPDATE ... WHERE candidate_id = ? AND candidate_status =
        'candidate'` (§5.2) -- zero rows affected (returns False) means the row was
        already decided or superseded by a racing regenerate; this method never
        raises for that case. The caller (service.decide_candidate, WP N9) is what
        turns False into CandidateAlreadyDecided/CandidateSuperseded."""
        ...

    def supersede_undecided(self, run_id: str, *, below_generation: int, now: str) -> int:
        """Regenerate step 1 (§5.2): marks every still-`candidate` row of this run
        with `generation < below_generation` as `candidate_status='superseded'` and
        returns the count changed. Rows already `accepted`/`rejected` are untouched
        -- a decision is frozen, never superseded by a later regenerate."""
        ...

    def write_themes(self, run_id: str, themes: list[dict], *, now: str) -> None:
        """Idempotent upsert by `theme_id` (`f"{run_id}:G{generation}:TH{ordinal}"`),
        same never-prune/never-delete discipline as `write_candidates` -- a prior
        generation's themes stay in Delta, distinguished by `generation` and
        `superseded`."""
        ...

    def list_themes(self, run_id: str) -> list[dict]:
        ...

    def put_test_line_values(self, run_id: str, rows: list[dict]) -> None:
        """§5.3: idempotent replace-per-run, the same MERGE + prune shape as
        `write_flagged_rows`/`write_run_metrics` -- `rows` (each {test_id, source,
        row_key, line_key, spend_amount, excess_amount}) becomes this run's entire
        test_line_values set. `prioritise` writes it for every plan test with
        flagged rows on an amount-bearing source; `finalise`'s deterministic
        headline recompute (`exposure.headline`) reads it back via
        `list_test_line_values`."""
        ...

    def list_test_line_values(self, run_id: str) -> list[dict]:
        ...

    # ── uploaded files (P5) ──────────────────────────────────────────────

    def record_uploaded_file(self, row: dict) -> dict:
        ...

    def update_uploaded_file(
        self, upload_id: str, *, status: str, row_count: int | None = None,
        columns_json: str | None = None, error: str | None = None
    ) -> None:
        ...

    def get_uploaded_file(self, upload_id: str) -> dict | None:
        ...

    def list_uploaded_files(self, engagement_id: str | None = None) -> list[dict]:
        ...

    # ── LLM call ledger (independent review 2026-09-24 item 3) ──────────────

    def record_llm_call(self, row: dict) -> None:
        """MERGEs on call_id -- an idempotent write (CLAUDE.md §3
        non-negotiable 7: every call is logged synchronously, before the
        call returns; a retried write of the same call_id must not
        duplicate)."""
        ...

    def last_live_version(self, endpoint: str) -> str | None:
        """The served_model_version of the most recent llm_calls row for
        `endpoint` with source='live' and outcome='succeeded', or None if
        there is none yet."""
        ...

    def get_llm_cache(self, cache_key: str) -> dict | None:
        ...

    def find_llm_cache(
        self, prompt_sha256: str, endpoint: str, params_json: str,
        served_model_version: str | None = None,
    ) -> list[dict]:
        """Response-cache rows matching (prompt_sha256, endpoint,
        params_json). With `served_model_version` given, scoped to the full
        NN8 key (docs/specs/P6_P8_explorer_llm_design.md §3.6 steps 5-6) --
        at most one row, since cache_key is a deterministic hash of the
        4-tuple. Left as None, returns every cached version for this
        prompt/endpoint/params, ordered by created_at desc -- used only to
        resolve an ambiguous served version in replay mode (step 5)."""
        ...

    def put_llm_cache_if_absent(self, row: dict) -> bool:
        """Inserts only if cache_key is not already present; never updates.
        Returns True if a row was inserted, False if one already existed."""
        ...

    def list_llm_calls(self, run_id: str) -> list[dict]:
        ...

    # ── row-level LLM classification (independent review item 4; built,
    # switched off by ENABLE_ROW_LEVEL_LLM=false default) ───────────────────

    def write_classification_results(self, run_id: str, rows: list[dict]) -> None:
        """Idempotent replace-per-run, same shape as write_flagged_rows:
        `rows` (each {row_key, personal_expense, confidence, rationale,
        call_id}) becomes this run's entire t43_classifications set."""
        ...

    def list_classification_results(self, run_id: str) -> list[dict]:
        ...

    def acquire_lease(self, run_id: str, worker_id: str, *, ttl_s: float, now: str) -> bool:
        ...

    def renew_lease(self, run_id: str, worker_id: str, *, ttl_s: float, now: str) -> bool:
        ...

    def release_lease(self, run_id: str, worker_id: str) -> None:
        ...

    def expired_leases(self, now: str) -> list[str]:
        ...


class PromptRepository(Protocol):
    """Matches orchestrator.llm.prompts.FilePromptRepository (WP N6:
    reconciled with the concrete implementation, which is keyed by
    `template_id` -- e.g. "explorer/planner", not by `task` -- and renders
    with `string.Template.substitute`, never an f-string or `str.format`
    the caller builds itself, docs/specs/P6_P8_explorer_llm_design.md
    §3.10)."""

    def get_template(self, template_id: str) -> Any:
        """Returns a `PromptTemplate(template_id, system, user, version)` --
        `version` is the sha256 over the template files' paths and bytes."""
        ...

    def template_set_version(self, template_ids: list[str]) -> str:
        """The hash over the union of `template_ids`' files -- what a run's
        fingerprint's `prompt_template_version` is built from."""
        ...

    def render(self, template_id: str, **params) -> list[dict]:
        """Renders one template into chat messages `[{"role": "system", ...},
        {"role": "user", ...}]`. Raises on a placeholder the caller did not
        supply -- never a literal `$name` left in a rendered prompt."""
        ...


class TracingAdapter(Protocol):
    def start_run(self, run_id: str) -> str:
        ...

    def start_span(self, *, run_id: str, node_name: str) -> str:
        ...

    def end_span(self, span_id: str, *, outcome: str, attributes: dict | None = None) -> None:
        ...

    def end_run(self, run_id: str, *, status: str) -> None:
        """Terminates the ONE parent MLflow run for this pipeline run
        (CLAUDE.md §2.3) with `status` in {"FINISHED", "FAILED", "KILLED"} --
        called at every RunState terminal transition (completed -> FINISHED,
        failed -> FAILED, interrupted -> KILLED) AND at each HITL pause gate
        (awaiting_confirmation/awaiting_signoff -> FINISHED, since this
        executor pass genuinely finished; a later resume finds the same
        parent run via start_run's idempotent lookup and reuses its id,
        never creating a duplicate, and a later end_run call updates its
        final status again). A parent run left RUNNING for a paused run's
        entire wait -- possibly hours, across an App restart -- is the
        defect this closes (P3 gap-audit review)."""
        ...


class NullTracing:
    """The default TracingAdapter when none is configured: every call is a
    documented no-op returning `""` (never raises, never records anything).
    `available=True` is deliberate -- distinct from a real adapter's own
    "unavailable" mode (e.g. MLflowTracingAdapter when `mlflow` cannot be
    imported, orchestrator/adapters/tracing_mlflow.py), which reports
    `available=False`/`unavailable_reason` so the pipeline logs ONE "tracing
    unavailable" trace_event (CLAUDE.md §2.3, non-negotiable 13: never
    silently pretend). A caller that never wired a tracing adapter at all
    (most tests, and any environment before this feature was wired) gets
    silence, not a spurious warning about a feature it never asked for."""

    available = True
    unavailable_reason: str | None = None

    def start_run(self, run_id: str, **kwargs: Any) -> str:
        return ""

    def start_span(self, *, run_id: str, node_name: str) -> str:
        return ""

    def end_span(self, span_id: str, *, outcome: str, attributes: dict | None = None) -> None:
        return None

    def end_run(self, run_id: str, *, status: str) -> None:
        return None


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


@dataclass(frozen=True)
class TicketPreview:
    """A single tracker-neutral issue-tracker ticket preview (CLAUDE.md §8:
    submission to any issue tracker -- Jira, ServiceNow, whichever the
    audit team is actually on -- is not built). Every field here is named
    for what it is, not for a particular vendor's API, so swapping the
    tracker is an adapter change, never a field rename."""

    issue_id: str
    title: str
    severity: str
    description: str | None
    status: str


class IssueTrackerAdapter(Protocol):
    """Only one implementation exists today
    (orchestrator.adapters.issue_tracker_preview.PreviewOnlyIssueTracker):
    preview() only, never submits anything (CLAUDE.md §8, NN13 -- no fake
    successful integration). A real tracker (ServiceNow is the one named so
    far) becomes a second implementation of this same Protocol when
    submission is actually built; nothing calling preview() needs to
    change."""

    def preview(self, issues: list[dict]) -> list[TicketPreview]:
        ...

    def submit(self, issues: list[dict]) -> list[dict]:
        """Declared, not implemented by any adapter yet."""
        ...


class Executor(Protocol):
    def start(self, run_id: str, phase: str) -> None:
        ...
