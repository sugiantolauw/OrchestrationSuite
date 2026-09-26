"""The `narrate` node's per-item generate/validate/repair/fallback loop (P6
WP N7, docs/specs/P6_narration_design.md §3.5, §4.1, §6.1). One
`narrate_<task>` function per narration task (§4.1's table, minus
`find_candidates` -- N8's task; this WP leaves a clean hook, never calling
it): each builds its own payload+table(s) via `orchestrator.narration.
payloads`, runs the shared `_generate_item` loop below, and persists every
resulting field through `upsert_narrative` (idempotent by `narrative_id`,
CLAUDE.md §2.3 rule 1 -- a re-executed `narrate` for the same generation
overwrites its own rows, never appends).

The shared loop (§3.5):

    generate (seq 2k+1) -- gateway.call() with this task's schema
      -> status "unavailable" -> fallback_unavailable, no repair, and this
         task's ROLE is marked broken for the rest of this narrate()
         execution (the circuit breaker: every later item whose task
         resolves to that same primary role skips calling altogether --
         "there is no llm_calls row, because no call was made")
      -> status "ok" but the caller's own `validate_fn` rejects it (or
         status "invalid_output", the gateway's own JSON-Schema failure)
         -> repair (seq 2k+2), same task/schema, `repair_user.md` listing
            what broke
           -> valid -> model_repaired
           -> invalid/unavailable -> fallback_invalid (unavailable on the
              repair attempt also breaks the circuit for later items)
      -> status "ok" and valid -> model

Budget per item: at most 2 logical calls (`seq`) x the gateway's own 2
transport attempts each (§3.5's own arithmetic; the transport retries are
`LLMGateway`'s concern, WP N6, not this module's).

Every field's `narratives.template_text` keeps the model's TYPED placeholder
form (`{class:name}`), never a rendered value (§6.1's own DDL comment:
"validated text WITH typed placeholders ... null for fallbacks") -- a
fallback row is persisted with `template_text=None` for EVERY task,
including exec_summary; the deterministic fallback content itself is a
resolver-time concern (`orchestrator.narration.resolve.effective_prose`,
WP N10), not something this module stores. `render()` is still called once
on every accepted (`model`/`model_repaired`) field before it is persisted --
not because the rendered form is stored, but because CLAUDE.md non-
negotiable 14 says a validator/renderer disagreement must fail loudly rather
than silently persist text nothing can actually render later; `validate_
prose` is supposed to guarantee `render()` succeeds, so this is a defensive
check, not a formatting step.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Callable, Iterable

from orchestrator.config import NODE_MODELS
from orchestrator.llm.gateway import CallContext
from orchestrator.llm.tasks import FALLBACK_ROLE, TASK_PROFILES
from orchestrator.narration.payloads import (
    build_caption_payload,
    build_exec_summary_payload,
    build_finding_payload,
    build_finding_table,
    build_priority_payload,
    build_profile_payload,
    build_remediation_payload,
    build_synthesis_payload,
    finding_key,
    identifiers_for_findings,
)
from orchestrator.narration.placeholders import NarrationConfigError, PlaceholderEntry, render, scan_placeholders
from orchestrator.narration.schemas import (
    chart_captions_schema,
    exec_summary_schema,
    finding_narration_schema,
    finding_synthesis_schema,
    priority_rationale_schema,
    profile_narrative_schema,
    remediation_schema,
)
from orchestrator.narration.validate import required_exec_summary_placeholders, validate_prose, validate_themes

__all__ = [
    "RunnerContext",
    "NarrationOutcome",
    "narrative_id",
    "narrate_profile",
    "narrate_finding",
    "narrate_synthesis",
    "narrate_priority",
    "narrate_remediation",
    "narrate_exec_summary",
    "narrate_captions",
]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def narrative_id(run_id: str, target_kind: str, target_id: str, field: str) -> str:
    """§6.1: `narrative_id = sha256(run_id|target_kind|target_id|field)[:32]`
    -- deliberately NOT keyed on `generation`, so a later generation's
    `upsert_narrative` for the same (run, target, field) overwrites the SAME
    row (`narratives` holds the CURRENT version per narrated field; history
    lives in `narrative_edits`, not here)."""
    import hashlib

    return hashlib.sha256(f"{run_id}|{target_kind}|{target_id}|{field}".encode("utf-8")).hexdigest()[:32]


@dataclass
class RunnerContext:
    gateway: Any
    prompts: Any  # orchestrator.narration.prompts.NarrationPromptRepository
    persistence: Any
    clock: Callable[[], str]
    run_id: str
    engagement_id: str | None
    actor: str
    generation: int
    pii_columns_masked: list[str]
    node_name: str = "narrate"
    execution_key: str | None = None
    # Circuit breaker (§3.5), keyed on (primary_role, fallback_role_or_None)
    # -- NOT on the primary role alone. Two tasks sharing a primary role but
    # NOT a fallback role (e.g. `find`, pair (model_sonnet, None), vs
    # `prioritise`, pair (model_sonnet, model_gpt_oss)) must be judged
    # independently: `find`'s primary going down proves nothing about
    # whether `prioritise`'s FALLBACK would also fail, so marking every
    # model_sonnet-primary task broken off `find` alone would silently skip
    # a `prioritise`/`act` call that GPT-OSS could still have answered
    # (defeating CLAUDE.md §6's fallback rule for those three tasks). A pair
    # is added only once GATEWAY.CALL() itself -- which tries the fallback
    # internally when one is configured -- has exhausted it and returned
    # "unavailable" for that exact pair.
    dead_role_pairs: set[tuple[str, str | None]] = dataclass_field(default_factory=set)
    origin_counts: dict[str, int] = dataclass_field(default_factory=dict)
    _seq_counters: dict[str, int] = dataclass_field(default_factory=dict)
    # Perf review 2026-09-25: `narrate` now runs its independent items
    # (orchestrator.nodes.narration._run_stage_one) through a bounded thread
    # pool, so every mutable field above this line (`dead_role_pairs`,
    # `origin_counts`, `_seq_counters`) can be touched by more than one
    # worker thread at once. A plain dict/set get-then-set is NOT atomic
    # across threads (only a single bytecode-level op is, under the GIL) --
    # every mutation goes through the three methods below, each holding this
    # lock for the whole read-modify-write. Never acquired by a caller
    # directly; `next_seq`/`mark_role_pair_dead`/`is_role_pair_dead`/
    # `record_origin` are the only entry points.
    _lock: threading.Lock = dataclass_field(default_factory=threading.Lock, repr=False, compare=False)
    # P6 WP N10 (§5.5 point 2): `narrative_id`s the caller (the `narrate`
    # node) found already at origin='human_edit' when this RunnerContext was
    # built, i.e. BEFORE this execution wrote anything -- `_persist` checks
    # this, never a live re-query, so a human_edit `_persist` skips for a
    # field this SAME `narrate()` execution has not yet reached only because
    # it was already human_edit at the start, never because a sibling field
    # earlier in this same call happened to write one (which cannot happen:
    # `_persist` never writes origin='human_edit', only `edit_narrative`
    # does, from a different module entirely).
    existing_human_edited: frozenset[str] = dataclass_field(default_factory=frozenset)

    def next_seq(self, task: str) -> tuple[int, int]:
        with self._lock:
            k = self._seq_counters.get(task, 0)
            self._seq_counters[task] = k + 1
            return 2 * k + 1, 2 * k + 2

    def mark_role_pair_dead(self, pair: tuple[str, str | None]) -> None:
        with self._lock:
            self.dead_role_pairs.add(pair)

    def is_role_pair_dead(self, pair: tuple[str, str | None]) -> bool:
        with self._lock:
            return pair in self.dead_role_pairs

    def record_origin(self, origin: str) -> None:
        with self._lock:
            self.origin_counts[origin] = self.origin_counts.get(origin, 0) + 1

    def call_ctx(self) -> CallContext:
        return CallContext(
            run_id=self.run_id, engagement_id=self.engagement_id, node_name=self.node_name,
            execution_key=self.execution_key, actor=self.actor,
            pii_columns_masked=self.pii_columns_masked, pii_whitelist=[],
        )


@dataclass(frozen=True)
class NarrationOutcome:
    origin: str  # model | model_repaired | fallback_invalid | fallback_unavailable
    parsed: dict | None
    call_ids: list[str]
    served_model_version: str | None
    violations_payload: list[dict] | None
    # Quality review 2026-09-25: for a BATCHED task (`narrate_priority`,
    # `narrate_remediation` -- one call answers for every item at once),
    # `origin`/`parsed` above describe the call as a WHOLE: a single item's
    # violation makes the whole batch "fallback_invalid" and discards
    # `parsed` entirely, which would needlessly throw away every OTHER
    # item's perfectly valid text. These three fields carry the last
    # structurally-parseable JSON response (whichever of generate/repair
    # produced it) even when the batch as a whole still fails, so a batched
    # caller can re-validate each item's own text against its own table and
    # keep the ones that are individually clean. A single-item caller
    # (`narrate_finding`, `narrate_profile`, ...) ignores these three --
    # `parsed`/`origin` alone already give it the right all-or-nothing
    # answer for its one target.
    attempted_parsed: dict | None = None
    attempted_served_model_version: str | None = None
    attempted_origin: str | None = None  # "model" | "model_repaired", whichever round attempted_parsed came from


def _generation_line(generation: int) -> str:
    if not generation:
        return ""
    return f"Regeneration request: write a fresh version. Generation {generation}."


def _validate_field(
    text: str, table: dict[str, PlaceholderEntry], *, field: str, allowed_identifiers: Iterable[str] = (),
    require_coverage: bool = False, required_placeholders: Iterable[str] | None = None,
) -> list[dict]:
    result = validate_prose(
        text, table, field=field, origin="model", allowed_identifiers=allowed_identifiers,
        require_coverage=require_coverage, required_placeholders=required_placeholders,
    )
    return [
        {"rule_id": v.rule_id, "field": field, "excerpt": (v.text or v.message)[:80]}
        for v in result.violations
    ]


def _generate_item(
    rc: RunnerContext, *, task: str, payload: dict, schema: dict, extra_params: dict,
    validate_fn: Callable[[dict], tuple[bool, list[dict]]], seq_pair: tuple[int, int] | None = None,
) -> NarrationOutcome:
    role = NODE_MODELS[task]
    pair = (role, FALLBACK_ROLE.get(task))
    if rc.is_role_pair_dead(pair):
        # Circuit breaker (§3.5): this exact primary/fallback pair already
        # returned "unavailable" once this narrate() execution -- no call,
        # no llm_calls row. Under concurrency (orchestrator.nodes.narration
        # perf review 2026-09-25) this only stops a call not yet DISPATCHED
        # -- an in-flight call started before the breaker tripped still
        # finishes and logs its own row, exactly as the design's "in-flight
        # ones finish/log" requires.
        return NarrationOutcome("fallback_unavailable", None, [], None, None)

    # `seq_pair` (perf review 2026-09-25): the "find" task is the only one
    # `narrate()` calls more than once, and it now dispatches those calls
    # concurrently -- `rc.next_seq("find")` would then be racing across
    # worker threads, making the seq assigned to a given finding depend on
    # thread-scheduling order rather than the run's own deterministic
    # finding order (breaking G9/replay). The caller precomputes every
    # item's seq pair sequentially, in the SAME deterministic order the
    # single-threaded code always used, before dispatching any worker.
    seq_gen, seq_repair = seq_pair if seq_pair is not None else rc.next_seq(task)
    ptver = rc.prompts.template_set_version()
    messages = rc.prompts.render_task(
        task, payload_json=_canonical_json(payload), generation_line=_generation_line(rc.generation),
        **extra_params,
    )
    result = rc.gateway.call(
        task=task, seq=seq_gen, messages=messages, desired_params=TASK_PROFILES[task].desired_params,
        schema=schema, ctx=rc.call_ctx(), prompt_template_id=f"narration/{task}", prompt_template_version=ptver,
    )
    if result.status == "unavailable":
        # Perf review 2026-09-25: only a PERMANENT unavailability (a real
        # 403/404/rate-limit-0/unconfigured-endpoint, `LLMResult.permanent`)
        # trips the breaker for every other item sharing this role -- a
        # RateLimited/TransientModelError that exhausted its bounded
        # retries (`permanent=False`) means only THIS item could not get an
        # answer; a sibling item, not yet dispatched, still gets its own
        # attempt.
        if result.permanent:
            rc.mark_role_pair_dead(pair)
        return NarrationOutcome("fallback_unavailable", None, [result.call_id], None, None)

    call_ids = [result.call_id]
    # Best structurally-parseable response seen so far -- kept even once a
    # later round fails, so a batched caller can still re-validate each
    # item's own text if every round from here on turns out invalid.
    attempted_parsed: dict | None = None
    attempted_served_model_version: str | None = None
    attempted_origin: str | None = None
    if result.status == "ok":
        is_valid, violations = validate_fn(result.parsed)
        if is_valid:
            return NarrationOutcome("model", result.parsed, call_ids, result.served_model_version, None)
        attempted_parsed, attempted_served_model_version, attempted_origin = (
            result.parsed, result.served_model_version, "model",
        )
    else:
        violations = [{"rule_id": "N-SCHEMA", "field": None, "excerpt": (result.error or "")[:80]}]

    repair_messages = rc.prompts.render_repair(
        task, violations_json=_canonical_json(violations), previous_output=result.text or "",
        **extra_params,
    )
    repair_result = rc.gateway.call(
        task=task, seq=seq_repair, messages=repair_messages, desired_params=TASK_PROFILES[task].desired_params,
        schema=schema, ctx=rc.call_ctx(), prompt_template_id=f"narration/{task}/repair",
        prompt_template_version=ptver,
    )
    call_ids.append(repair_result.call_id)
    if repair_result.status == "unavailable":
        if repair_result.permanent:
            rc.mark_role_pair_dead(pair)
        return NarrationOutcome("fallback_unavailable", None, call_ids, None, None)
    if repair_result.status == "ok":
        is_valid2, violations2 = validate_fn(repair_result.parsed)
        if is_valid2:
            return NarrationOutcome(
                "model_repaired", repair_result.parsed, call_ids, repair_result.served_model_version, None,
            )
        violations = violations2
        attempted_parsed, attempted_served_model_version, attempted_origin = (
            repair_result.parsed, repair_result.served_model_version, "model_repaired",
        )
    else:
        violations = [{"rule_id": "N-SCHEMA", "field": None, "excerpt": (repair_result.error or "")[:80]}]

    return NarrationOutcome(
        "fallback_invalid", None, call_ids, None, violations,
        attempted_parsed=attempted_parsed,
        attempted_served_model_version=attempted_served_model_version,
        attempted_origin=attempted_origin,
    )


def _used_placeholder_names(texts: list[str], table: dict[str, PlaceholderEntry]) -> set[str]:
    used: set[str] = set()
    for t in texts:
        for span in scan_placeholders(t or ""):
            if span.valid_syntax and span.name in table:
                used.add(span.name)
    return used


def _persist(
    rc: RunnerContext, *, target_kind: str, target_id: str, field: str, origin: str,
    table: dict[str, PlaceholderEntry], text: str | None = None, list_text: list[str] | None = None,
    call_ids: list[str], served_model_version: str | None, violations_payload: list[dict] | None,
) -> str:
    now = rc.clock()
    nid = narrative_id(rc.run_id, target_kind, target_id, field)
    if nid in rc.existing_human_edited:
        # §5.5 point 2: "re-narrates every narrative whose current origin is
        # not human_edit" -- a regenerate's fresh model/repair/fallback
        # output for THIS field is discarded rather than persisted, so the
        # auditor's edit (and its version/history) is untouched. The call
        # that produced `origin`/`text`/`call_ids` above still happened
        # (this WP's call-budget invariant is unaffected, only the WRITE is
        # skipped) -- `nid` is still the correct id to hand back to the
        # caller (e.g. `narrate_finding`'s own return value, stored on
        # RunState), since it already resolves to the human-edited row.
        return nid
    if origin in ("model", "model_repaired"):
        texts = list_text if list_text is not None else [text]
        try:
            for t in texts:
                render(t, table)  # defensive: validate_prose must guarantee this succeeds (CLAUDE.md NN14)
        except NarrationConfigError as exc:
            # BUG-R5-1 backstop (independent review 2026-09-26): `validate_fn`
            # validated these texts against this exact `table` moments ago,
            # so this should never fire -- but if it ever does (a future
            # instance of the same "table drifted between validation and
            # persist" shape this bug's own root fix closed for the profile
            # narrative), CLAUDE.md NN14 and the WP's own rule ("any item
            # that fails after repair must become the labelled fallback,
            # never stored as model text") both apply here too. Falling
            # through to the fallback branch below is what makes that true:
            # never store an unfilled `{class:name}` placeholder, and never
            # crash the run over it.
            origin = "fallback_invalid"
            violations_payload = (violations_payload or []) + [
                {"rule_id": "N-G3", "field": field, "excerpt": str(exc)[:80]}
            ]
            sources = []
            template_text = None
        else:
            used = _used_placeholder_names(texts, table)
            sources = [
                {"placeholder": f"{{{table[n].cls}:{n}}}", "source_field": table[n].source_field, "unit": table[n].unit}
                for n in sorted(used)
            ]
            template_text = _canonical_json(list_text) if list_text is not None else text
    else:
        sources = []
        template_text = None
    row = {
        "narrative_id": nid, "run_id": rc.run_id, "engagement_id": rc.engagement_id,
        "target_kind": target_kind, "target_id": target_id, "field": field,
        "version": rc.generation + 1, "generation": rc.generation, "origin": origin,
        "template_text": template_text, "sources": sources, "call_ids": call_ids,
        "served_model_version": served_model_version, "violations": violations_payload,
        "updated_by": "model" if origin in ("model", "model_repaired") else "system",
        "updated_at": now,
    }
    rc.persistence.upsert_narrative(row)
    rc.record_origin(origin)
    return nid


# ── task-specific narrate_* functions ───────────────────────────────────────


def narrate_profile(rc: RunnerContext, state, skill) -> str | None:
    if not getattr(state, "profile_result", None):
        return None
    payload, table = build_profile_payload(state)
    schema = profile_narrative_schema()

    def validate_fn(parsed: dict) -> tuple[bool, list[dict]]:
        violations: list[dict] = []
        for p in parsed.get("paragraphs", []):
            violations += _validate_field(p, table, field="profile_paragraph")
        return not violations, violations

    outcome = _generate_item(rc, task="profile", payload=payload, schema=schema, extra_params={}, validate_fn=validate_fn)
    paragraphs = outcome.parsed.get("paragraphs") if outcome.parsed else None
    return _persist(
        rc, target_kind="profile", target_id="run", field="profile", origin=outcome.origin, table=table,
        list_text=paragraphs, call_ids=outcome.call_ids, served_model_version=outcome.served_model_version,
        violations_payload=outcome.violations_payload,
    )


def _allowed_identifiers(finding: dict) -> set[str]:
    return set(identifiers_for_findings([finding]))


def narrate_finding(
    rc: RunnerContext, finding: dict, *, skill, period: tuple[str, str] | None,
    seq_pair: tuple[int, int] | None = None,
) -> str:
    payload, table = build_finding_payload(finding, skill=skill, period=period)
    key = finding_key(finding)
    schema = finding_narration_schema(key)
    allowed = _allowed_identifiers(finding)
    # §3.2's own coverage rule: "observation must use every non-null cited
    # metric" -- `metrics_cited` only, never the threshold refs, exposure
    # amount or period dates `table` also carries (those exist so the model
    # MAY use them, not so it MUST); `validate_prose`'s own default (every
    # non-null table entry) would over-require coverage of all of those too.
    cited_names = frozenset(name for name in (finding.get("metrics_cited") or {}) if name in table)

    def validate_fn(parsed: dict) -> tuple[bool, list[dict]]:
        violations = _validate_field(
            parsed.get("observation", ""), table, field="observation", allowed_identifiers=allowed,
            require_coverage=True, required_placeholders=cited_names,
        )
        violations += _validate_field(parsed.get("recommendation", ""), table, field="recommendation", allowed_identifiers=allowed)
        for q in parsed.get("management_questions", []):
            violations += _validate_field(q, table, field="question", allowed_identifiers=allowed)
        return not violations, violations

    outcome = _generate_item(
        rc, task="find", payload=payload, schema=schema, extra_params={}, validate_fn=validate_fn,
        seq_pair=seq_pair,
    )
    finding_id = finding.get("finding_id") or finding.get("candidate_id") or key
    parsed = outcome.parsed or {}
    observation_id = _persist(
        rc, target_kind="finding", target_id=finding_id, field="observation", origin=outcome.origin, table=table,
        text=parsed.get("observation"), call_ids=outcome.call_ids, served_model_version=outcome.served_model_version,
        violations_payload=outcome.violations_payload,
    )
    _persist(
        rc, target_kind="finding", target_id=finding_id, field="recommendation", origin=outcome.origin, table=table,
        text=parsed.get("recommendation"), call_ids=outcome.call_ids, served_model_version=outcome.served_model_version,
        violations_payload=outcome.violations_payload,
    )
    _persist(
        rc, target_kind="finding", target_id=finding_id, field="management_questions", origin=outcome.origin,
        table=table, list_text=parsed.get("management_questions"), call_ids=outcome.call_ids,
        served_model_version=outcome.served_model_version, violations_payload=outcome.violations_payload,
    )
    return observation_id


def narrate_synthesis(rc: RunnerContext, findings: list[dict], *, skill) -> tuple[list[dict], dict[str, dict]]:
    if not findings:
        return [], {}
    payload, tables = build_synthesis_payload(findings, skill=skill)
    keys = [finding_key(f) for f in findings]
    schema = finding_synthesis_schema(keys)
    findings_by_key = {finding_key(f): f for f in findings}
    allowed = identifiers_for_findings(findings)

    def validate_fn(parsed: dict) -> tuple[bool, list[dict]]:
        violations: list[dict] = []
        struct = validate_themes(parsed.get("themes", []), valid_finding_keys=keys)
        violations += [{"rule_id": v.rule_id, "field": "themes", "excerpt": v.message[:80]} for v in struct]
        for theme in parsed.get("themes", []):
            member_keys = theme.get("finding_keys", [])
            theme_table: dict[str, PlaceholderEntry] = {}
            for k in member_keys:
                theme_table.update(tables.get(k, {}))
            violations += _validate_field(theme.get("title", ""), theme_table, field="theme_title", allowed_identifiers=allowed)
            violations += _validate_field(theme.get("summary", ""), theme_table, field="theme_summary", allowed_identifiers=allowed)
            violations += _validate_field(
                theme.get("root_cause_hypothesis", ""), theme_table, field="root_cause", allowed_identifiers=allowed,
            )
            for robs in theme.get("review_observations", []):
                violations += _validate_field(robs, theme_table, field="review_observation", allowed_identifiers=allowed)
        for prop in parsed.get("severity_proposals", []):
            table = tables.get(prop.get("finding_key"), {})
            violations += _validate_field(prop.get("reason", ""), table, field="rationale", allowed_identifiers=allowed)
        return not violations, violations

    outcome = _generate_item(
        rc, task="find_synthesis", payload=payload, schema=schema, extra_params={}, validate_fn=validate_fn,
    )

    def _persist_theme(
        ordinal: int, theme: dict, *, origin: str, served_model_version: str | None,
    ) -> dict:
        theme_id = f"{rc.run_id}:G{rc.generation}:TH{ordinal}"
        member_keys = theme.get("finding_keys", [])
        finding_ids = [findings_by_key[k]["finding_id"] for k in member_keys if k in findings_by_key]
        theme_table: dict[str, PlaceholderEntry] = {}
        for k in member_keys:
            theme_table.update(tables.get(k, {}))
        _persist(
            rc, target_kind="theme", target_id=theme_id, field="title", origin=origin, table=theme_table,
            text=theme.get("title", ""), call_ids=outcome.call_ids, served_model_version=served_model_version,
            violations_payload=None,
        )
        _persist(
            rc, target_kind="theme", target_id=theme_id, field="summary", origin=origin, table=theme_table,
            text=theme.get("summary", ""), call_ids=outcome.call_ids, served_model_version=served_model_version,
            violations_payload=None,
        )
        _persist(
            rc, target_kind="theme", target_id=theme_id, field="root_cause", origin=origin, table=theme_table,
            text=theme.get("root_cause_hypothesis", ""), call_ids=outcome.call_ids,
            served_model_version=served_model_version, violations_payload=None,
        )
        review_observations = theme.get("review_observations", [])
        if review_observations:
            _persist(
                rc, target_kind="theme", target_id=theme_id, field="review_observations", origin=origin,
                table=theme_table, list_text=review_observations, call_ids=outcome.call_ids,
                served_model_version=served_model_version, violations_payload=None,
            )
        return {"theme_id": theme_id, "generation": rc.generation, "ordinal": ordinal, "finding_ids": finding_ids}

    def _theme_field_violations(theme: dict) -> list[dict]:
        member_keys = theme.get("finding_keys", [])
        theme_table: dict[str, PlaceholderEntry] = {}
        for k in member_keys:
            theme_table.update(tables.get(k, {}))
        violations = []
        violations += _validate_field(theme.get("title", ""), theme_table, field="theme_title", allowed_identifiers=allowed)
        violations += _validate_field(theme.get("summary", ""), theme_table, field="theme_summary", allowed_identifiers=allowed)
        violations += _validate_field(
            theme.get("root_cause_hypothesis", ""), theme_table, field="root_cause", allowed_identifiers=allowed,
        )
        for robs in theme.get("review_observations", []):
            violations += _validate_field(robs, theme_table, field="review_observation", allowed_identifiers=allowed)
        return violations

    themes_out: list[dict] = []
    severity_proposals: dict[str, dict] = {}
    if outcome.origin in ("model", "model_repaired"):
        parsed = outcome.parsed
        for ordinal, theme in enumerate(parsed.get("themes", []), start=1):
            themes_out.append(
                _persist_theme(ordinal, theme, origin=outcome.origin, served_model_version=outcome.served_model_version)
            )
        for prop in parsed.get("severity_proposals", []):
            fk = prop.get("finding_key")
            finding = findings_by_key.get(fk)
            if finding is not None and prop.get("proposed_severity") != finding.get("severity"):
                severity_proposals[fk] = {
                    "proposed_severity": prop.get("proposed_severity"),
                    "proposed_severity_reason": prop.get("reason"),
                }
    elif outcome.origin == "fallback_invalid" and outcome.attempted_parsed:
        # Independent narration-content review 2026-09-25: the live run's
        # synthesis fell back to the deterministic "no themes" text even
        # though the repair round DID produce a structurally-valid response
        # -- one theme's own prose tripped a rule (N-Q1/N-S2/N-S4/...) and
        # that single violation discarded every OTHER theme too, the exact
        # shape `_persist_batch_items` above already fixed for
        # `narrate_priority`/`narrate_remediation` batches. Mirror that fix
        # here: when the STRUCTURAL checks (`validate_themes` -- theme
        # count, cross-theme finding-key membership, review-observation
        # caps) pass on the attempted output, re-validate each theme's own
        # fields against its own table and keep only the individually-clean
        # ones; a theme that is still wrong on its own is dropped, not
        # substituted with fallback text (a theme has no "template" prose to
        # fall back to, unlike a finding's recommendation). This never
        # loosens what counts as a violation -- `_validate_field` is the
        # same check `validate_fn` already ran on the whole batch; it only
        # stops one bad theme from discarding every good one. Structural
        # violations are whole-batch by nature (which theme "owns" a
        # duplicate finding-key membership is not decidable per-theme) and
        # still fall back to the existing all-or-nothing path below.
        attempted = outcome.attempted_parsed
        candidate_themes = attempted.get("themes", [])
        struct_violations = validate_themes(candidate_themes, valid_finding_keys=keys)
        if not struct_violations:
            for ordinal, theme in enumerate(candidate_themes, start=1):
                if _theme_field_violations(theme):
                    continue  # drop only this theme; siblings that validate on their own are kept
                themes_out.append(
                    _persist_theme(
                        ordinal, theme, origin=outcome.attempted_origin,
                        served_model_version=outcome.attempted_served_model_version,
                    )
                )
            for prop in attempted.get("severity_proposals", []):
                fk = prop.get("finding_key")
                finding = findings_by_key.get(fk)
                if finding is None:
                    continue
                table = tables.get(fk, {})
                if _validate_field(prop.get("reason", ""), table, field="rationale", allowed_identifiers=allowed):
                    continue
                if prop.get("proposed_severity") != finding.get("severity"):
                    severity_proposals[fk] = {
                        "proposed_severity": prop.get("proposed_severity"),
                        "proposed_severity_reason": prop.get("reason"),
                    }

    if not themes_out and not severity_proposals:
        # §3.5: "synthesis -> no themes" on fallback -- one narrative row
        # records why, at run scope (no theme_id yet exists to attach it to).
        # Reached both by a genuinely unavailable/unparseable call AND by a
        # salvage attempt above that still found nothing individually clean
        # to keep.
        _persist(
            rc, target_kind="theme", target_id="run", field="summary", origin=outcome.origin, table={},
            call_ids=outcome.call_ids, served_model_version=None, violations_payload=outcome.violations_payload,
        )

    return themes_out, severity_proposals


def _keyed_ids_by_target(items: list[dict]) -> dict[str, str]:
    return {finding_key(it): (it.get("finding_id") or it.get("candidate_id") or finding_key(it)) for it in items}


def _persist_batch_items(
    rc: RunnerContext, *, keyed_targets: dict[str, str], tables: dict[str, dict[str, PlaceholderEntry]],
    outcome: NarrationOutcome, response_field: str, target_field: str, validate_field_name: str,
) -> dict[str, str]:
    """Shared persistence for a BATCHED narration call (`narrate_priority`,
    `narrate_remediation`): one gateway call answers for every item, so
    `outcome.origin`/`outcome.parsed` describe the call as a whole -- a
    single item's own violation makes `_generate_item` discard the WHOLE
    batch (§ `NarrationOutcome.attempted_parsed`'s own docstring). When that
    happens here, re-validate each item's own text against its own table:
    an item that is clean on its own keeps the model's text (origin =
    whichever round -- generate or repair -- `attempted_parsed` came from);
    only an item that is STILL wrong on its own falls back to template text.
    This never loosens what counts as a violation -- `_validate_field` is
    the exact same check `validate_fn` already ran; it only stops one bad
    item from discarding every good one."""
    out: dict[str, str] = {}
    if outcome.origin == "fallback_invalid" and outcome.attempted_parsed:
        entries_by_key = {e.get("key"): e for e in outcome.attempted_parsed.get("items", [])}
        for key, target_id in keyed_targets.items():
            entry = entries_by_key.get(key)
            if entry is None:
                out[target_id] = _persist(
                    rc, target_kind="finding", target_id=target_id, field=target_field, origin="fallback_invalid",
                    table={}, text=None, call_ids=outcome.call_ids, served_model_version=None,
                    violations_payload=[{"rule_id": "N-X1", "field": "items", "excerpt": f"missing key {key!r}"[:80]}],
                )
                continue
            text = entry.get(response_field, "")
            item_violations = _validate_field(text, tables.get(key, {}), field=validate_field_name)
            if item_violations:
                out[target_id] = _persist(
                    rc, target_kind="finding", target_id=target_id, field=target_field, origin="fallback_invalid",
                    table={}, text=None, call_ids=outcome.call_ids, served_model_version=None,
                    violations_payload=item_violations,
                )
            else:
                out[target_id] = _persist(
                    rc, target_kind="finding", target_id=target_id, field=target_field, origin=outcome.attempted_origin,
                    table=tables.get(key, {}), text=text, call_ids=outcome.call_ids,
                    served_model_version=outcome.attempted_served_model_version, violations_payload=None,
                )
        return out

    by_key = {e["key"]: e.get(response_field) for e in (outcome.parsed or {}).get("items", [])} if outcome.parsed else {}
    for key, target_id in keyed_targets.items():
        out[target_id] = _persist(
            rc, target_kind="finding", target_id=target_id, field=target_field, origin=outcome.origin,
            table=tables.get(key, {}), text=by_key.get(key), call_ids=outcome.call_ids,
            served_model_version=outcome.served_model_version, violations_payload=outcome.violations_payload,
        )
    return out


def narrate_priority(rc: RunnerContext, items: list[dict], *, skill, period: tuple[str, str] | None) -> dict[str, str]:
    if not items:
        return {}
    payload, tables = build_priority_payload(items, skill=skill, period=period)
    keys = [finding_key(it) for it in items]
    schema = priority_rationale_schema(keys)

    def validate_fn(parsed: dict) -> tuple[bool, list[dict]]:
        violations: list[dict] = []
        seen: set[str] = set()
        for entry in parsed.get("items", []):
            k = entry.get("key")
            if k in seen:
                violations.append({"rule_id": "N-X1", "field": "items", "excerpt": f"duplicate key {k}"[:80]})
                continue
            seen.add(k)
            violations += _validate_field(entry.get("rationale", ""), tables.get(k, {}), field="rationale")
        missing = set(keys) - seen
        if missing:
            violations.append({"rule_id": "N-X1", "field": "items", "excerpt": f"missing key(s) {sorted(missing)}"[:80]})
        return not violations, violations

    outcome = _generate_item(rc, task="prioritise", payload=payload, schema=schema, extra_params={}, validate_fn=validate_fn)
    return _persist_batch_items(
        rc, keyed_targets=_keyed_ids_by_target(items), tables=tables, outcome=outcome,
        response_field="rationale", target_field="rationale", validate_field_name="rationale",
    )


def narrate_remediation(rc: RunnerContext, items: list[dict], *, skill, period: tuple[str, str] | None) -> dict[str, str]:
    if not items:
        return {}
    payload, tables = build_remediation_payload(items, skill=skill, period=period)
    keys = [finding_key(it) for it in items]
    schema = remediation_schema(keys)

    def validate_fn(parsed: dict) -> tuple[bool, list[dict]]:
        violations: list[dict] = []
        seen: set[str] = set()
        for entry in parsed.get("items", []):
            k = entry.get("key")
            if k in seen:
                violations.append({"rule_id": "N-X1", "field": "items", "excerpt": f"duplicate key {k}"[:80]})
                continue
            seen.add(k)
            # No dedicated "remediation" FIELD_LENGTH_CAPS entry -- reuses
            # the "recommendation" field's rules (same 600-char cap, same
            # observation-type language rules do NOT apply, matching how a
            # management action reads: an instruction, not a finding).
            violations += _validate_field(entry.get("remediation", ""), tables.get(k, {}), field="recommendation")
        missing = set(keys) - seen
        if missing:
            violations.append({"rule_id": "N-X1", "field": "items", "excerpt": f"missing key(s) {sorted(missing)}"[:80]})
        return not violations, violations

    outcome = _generate_item(rc, task="act", payload=payload, schema=schema, extra_params={}, validate_fn=validate_fn)
    return _persist_batch_items(
        rc, keyed_targets=_keyed_ids_by_target(items), tables=tables, outcome=outcome,
        response_field="remediation", target_field="remediation", validate_field_name="recommendation",
    )


def narrate_exec_summary(
    rc: RunnerContext, state, findings: list[dict], metrics: dict[str, dict], *,
    catalogue_tests: list[dict], themes: list[dict] = (), skill=None,
) -> str | None:
    built = build_exec_summary_payload(
        state, findings, metrics, catalogue_tests=catalogue_tests, themes=themes, skill=skill,
    )
    if built is None:  # G10: zero rule findings -- no call, the deterministic clean-run text is used at export
        return None
    payload, table = built
    schema = exec_summary_schema()

    def validate_fn(parsed: dict) -> tuple[bool, list[dict]]:
        violations: list[dict] = []
        paragraphs = parsed.get("paragraphs", [])
        for p in paragraphs:
            violations += _validate_field(p, table, field="exec_paragraph")
        used = _used_placeholder_names(paragraphs, table)
        missing = required_exec_summary_placeholders(table) - used
        if missing:
            violations.append({"rule_id": "N-C1", "field": "paragraphs", "excerpt": f"missing {sorted(missing)}"[:80]})
        return not violations, violations

    outcome = _generate_item(rc, task="export_summary", payload=payload, schema=schema, extra_params={}, validate_fn=validate_fn)
    paragraphs = outcome.parsed.get("paragraphs") if outcome.parsed else None
    return _persist(
        rc, target_kind="run", target_id="run", field="exec_summary", origin=outcome.origin, table=table,
        list_text=paragraphs, call_ids=outcome.call_ids, served_model_version=outcome.served_model_version,
        violations_payload=outcome.violations_payload,
    )


def narrate_captions(rc: RunnerContext, charts: list[dict], metrics: dict[str, dict]) -> dict[str, str]:
    if not charts:
        return {}
    payload, tables = build_caption_payload(charts, metrics)
    chart_ids = [c["chart_id"] for c in charts]
    schema = chart_captions_schema(chart_ids)

    def validate_fn(parsed: dict) -> tuple[bool, list[dict]]:
        violations: list[dict] = []
        seen: set[str] = set()
        for entry in parsed.get("captions", []):
            cid = entry.get("chart_id")
            if cid in seen:
                violations.append({"rule_id": "N-X1", "field": "captions", "excerpt": f"duplicate chart_id {cid}"[:80]})
                continue
            seen.add(cid)
            violations += _validate_field(entry.get("caption", ""), tables.get(cid, {}), field="caption")
        missing = set(chart_ids) - seen
        if missing:
            violations.append({"rule_id": "N-X1", "field": "captions", "excerpt": f"missing chart id(s) {sorted(missing)}"[:80]})
        return not violations, violations

    outcome = _generate_item(rc, task="export_caption", payload=payload, schema=schema, extra_params={}, validate_fn=validate_fn)
    by_id = {e["chart_id"]: e.get("caption") for e in (outcome.parsed or {}).get("captions", [])} if outcome.parsed else {}
    out: dict[str, str] = {}
    for cid in chart_ids:
        out[cid] = _persist(
            rc, target_kind="chart", target_id=cid, field="caption", origin=outcome.origin, table=tables.get(cid, {}),
            text=by_id.get(cid), call_ids=outcome.call_ids, served_model_version=outcome.served_model_version,
            violations_payload=outcome.violations_payload,
        )
    return out


# `act`'s own resolution (§2: "An action's description is the effective
# remediation draft, not the raw recommendation") is now WP N10's full
# cross-target `effective_prose` resolver,
# `orchestrator.narration.resolve.effective_remediation` -- this module's own
# narrow WP N7 stand-in (`effective_remediation_text`) has been replaced by
# it, not kept alongside it (CLAUDE.md §1 #1: number/text safety by
# construction, one formatter, one resolver).
