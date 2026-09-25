"""LLMGateway: the one place a node calls a model (independent review
2026-09-24 item 3; docs/specs/P6_P8_explorer_llm_design.md §3.6). Every call
goes: resolve role -> filter params through the role's recorded capability
matrix -> check the response cache -> call the ModelClient (with bounded,
Retry-After-honouring backoff on a rate limit / transient failure --
`max_transport_attempts`, perf review 2026-09-25) -> validate the output
against a JSON Schema if one was given (with one client-side retry) -> log
to `llm_calls`
SYNCHRONOUSLY BEFORE RETURNING (CLAUDE.md §3 non-negotiable 7) -> cache a
successful response for replay (non-negotiable 8).

A node never talks to a ModelClient directly, never chooses which
parameters to send, and never decides what "unavailable" means -- all of
that lives here, once, so every node degrades the same way (CLAUDE.md §6
NN13: "LLM unavailable — deterministic output only").

WP N6 additions (docs/specs/P6_narration_design.md §6.3, §4.1):
- `call()` takes `prompt_template_id`/`prompt_template_version` per call
  (falling back to the values fixed at construction, for the one existing
  caller -- orchestrator.nodes.fieldwork._build_classify_gateway -- that
  still fixes them there). Narration has one prompt template per task, not
  one per gateway instance.
- `LLM_CACHE_MODE=replay` (Settings.llm_cache_mode): a cache hit still
  returns the stored response; a miss logs outcome='replay_miss' and raises
  `LLMReplayMiss` -- never a live call. `live` (the default) keeps the
  existing behaviour.
- `FALLBACK_ROLE` (orchestrator.llm.tasks): when a task's primary role is
  unavailable, an allowed task retries once against its fallback role,
  logging a second `llm_calls` row under that role's `endpoint_role` --
  that second row, next to the primary role's `unavailable` row, IS the
  record of the degradation (no separate boolean column exists for it).
  Skipped when the fallback role resolves to the same endpoint as the
  primary (the development-workspace override)."""

from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import jsonschema

from orchestrator.llm.capabilities import (
    filter_params,
    load_capabilities,
    role_config,
    schema_strict_block_reason,
)
from orchestrator.llm.errors import (
    LLMConfigError,
    LLMLoggingError,
    LLMReplayMiss,
    ModelUnavailable,
    RateLimited,
    TransientModelError,
    TruncatedOutput,
)
from orchestrator.llm.tasks import FALLBACK_ROLE as _DEFAULT_FALLBACK_ROLE


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _default_clock() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CallContext:
    run_id: str | None = None
    engagement_id: str | None = None
    node_name: str | None = None
    execution_key: str | None = None
    actor: str = "system"
    pii_columns_masked: list[str] = field(default_factory=list)
    pii_whitelist: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LLMResult:
    status: Literal["ok", "unavailable", "invalid_output"]
    text: str | None
    parsed: dict | None
    call_id: str
    source: Literal["live", "cache"] | None
    served_model_version: str | None
    error: str | None
    # Perf review 2026-09-25: meaningful only when status == "unavailable".
    # True (the default, for every OTHER status and for the pre-existing
    # 403/404/rate-limit-0/unconfigured-endpoint paths) means the endpoint
    # will not come back without operator action -- safe to trip
    # `orchestrator.narration.runner`'s circuit breaker for every other item
    # sharing the role. False means a RateLimited/TransientModelError
    # exhausted its bounded retries for THIS item alone; a sibling item may
    # still succeed, so the breaker must not trip on it.
    permanent: bool = True


class LLMGateway:
    def __init__(
        self,
        *,
        settings,
        client,
        persistence,
        node_models: dict[str, str],
        capabilities: dict | None = None,
        clock=_default_clock,
        retry_backoff_s: float = 5.0,
        timeout_s: float = 180.0,
        prompt_template_id: str = "none",
        prompt_template_version: str = "none",
        fallback_role: dict[str, str | None] | None = None,
        # Perf review 2026-09-25 (found live: a 4-way concurrent narration
        # run hit the workspace's QPS limit repeatedly; a single retry at a
        # flat backoff was not enough, and giving up after it wrongly
        # tripped the circuit breaker for every other item on the same
        # role). `max_transport_attempts` bounds how many times ONE logical
        # call retries a RateLimited/TransientModelError before giving up
        # as (non-permanent) unavailable -- 2 is the pre-existing behaviour
        # (one retry); `retry_backoff_max_s` caps the EXPONENTIAL backoff
        # `_call_live` computes when the endpoint's own response states no
        # `Retry-After` (the common case for Databricks Model Serving).
        max_transport_attempts: int = 3,
        retry_backoff_max_s: float = 30.0,
    ):
        self.settings = settings
        self.client = client
        self.persistence = persistence
        self.node_models = node_models
        self.capabilities = capabilities if capabilities is not None else load_capabilities()
        self.clock = clock
        self.retry_backoff_s = retry_backoff_s
        self.timeout_s = timeout_s
        self.max_transport_attempts = max_transport_attempts
        self.retry_backoff_max_s = retry_backoff_max_s
        # Construction-time defaults, kept for the one caller that still
        # fixes these at construction (orchestrator.nodes.fieldwork.
        # _build_classify_gateway) -- every other caller passes them per
        # call (see `call()` below).
        self.prompt_template_id = prompt_template_id
        self.prompt_template_version = prompt_template_version
        self.fallback_role = fallback_role if fallback_role is not None else _DEFAULT_FALLBACK_ROLE

    # ── public entry point ──────────────────────────────────────────────

    def call(
        self,
        *,
        task: str,
        seq: int,
        messages: list[dict],
        desired_params: dict,
        schema: dict | None = None,
        ctx: CallContext,
        prompt_template_id: str | None = None,
        prompt_template_version: str | None = None,
    ) -> LLMResult:
        role = self.node_models.get(task)
        if not role:
            raise LLMConfigError(f"no NODE_MODELS entry for task {task!r}")
        ptid = prompt_template_id if prompt_template_id is not None else self.prompt_template_id
        ptver = prompt_template_version if prompt_template_version is not None else self.prompt_template_version

        result = self._attempt(
            task=task, seq=seq, role=role, messages=messages, desired_params=desired_params,
            schema=schema, ctx=ctx, prompt_template_id=ptid, prompt_template_version=ptver,
        )
        if result.status == "unavailable":
            # CLAUDE.md §6 fallback rule (docs/specs/P6_narration_design.md
            # §4.1): only a task FALLBACK_ROLE names may degrade, and only
            # once, to a role whose endpoint actually differs from the
            # primary's (never a second attempt against the same endpoint
            # that just failed -- the development-workspace override where
            # MODEL_SONNET and MODEL_GPT_OSS point at the same endpoint).
            fallback_role = self.fallback_role.get(task)
            if fallback_role and fallback_role != role:
                primary_endpoint = getattr(self.settings, role, None)
                fallback_endpoint = getattr(self.settings, fallback_role, None)
                if fallback_endpoint and fallback_endpoint != primary_endpoint:
                    result = self._attempt(
                        task=task, seq=seq, role=fallback_role, messages=messages,
                        desired_params=desired_params, schema=schema, ctx=ctx,
                        prompt_template_id=ptid, prompt_template_version=ptver,
                    )
        return result

    # ── one role attempt: cache lookup (or replay-mode enforcement), then a
    # live call if this is not a cache hit ──────────────────────────────────

    def _attempt(
        self, *, task, seq, role, messages, desired_params, schema, ctx,
        prompt_template_id, prompt_template_version,
    ) -> LLMResult:
        endpoint = getattr(self.settings, role, None)

        if not endpoint:
            return self._log_and_return(
                task=task, seq=seq, role=role, endpoint=None, messages=messages,
                params_sent={}, params_dropped={k: "endpoint not configured" for k in desired_params},
                ctx=ctx, transport_attempt=1, outcome="unavailable",
                error_type="LLMConfigError", error_message=f"endpoint not configured for role {role!r}",
                status="unavailable", prompt_template_id=prompt_template_id,
                prompt_template_version=prompt_template_version,
            )

        sent, dropped = filter_params(self.capabilities, role, desired_params)
        final_messages = list(messages)
        supports_strict = role_config(self.capabilities, role).get("params", {}).get("json_schema_strict") == "supported"
        strict_block_reason = None
        if schema is not None and supports_strict:
            # A role whose matrix marks json_schema_strict "supported" can
            # still reject a PARTICULAR call's schema -- this endpoint's own
            # 128-total-properties ceiling (orchestrator.llm.capabilities.
            # schema_strict_block_reason; recorded 2026-09-25 after a live
            # 400 on `plan_repair`'s PLAN_PROPOSAL_SCHEMA, capabilities.yaml
            # max_schema_properties). When it does, this call falls back to
            # the same schema-in-prompt + client-side-validation path as a
            # role with no strict support at all, never a malformed request.
            strict_block_reason = schema_strict_block_reason(self.capabilities, role, schema)
            if strict_block_reason:
                supports_strict = False
        if schema is not None:
            if supports_strict:
                sent = dict(sent)
                sent["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": task, "strict": True, "schema": schema},
                }
            else:
                dropped = dict(dropped)
                dropped["response_format"] = (
                    strict_block_reason or "untested (json_schema_strict not supported for this role)"
                )
                final_messages = final_messages + [{
                    "role": "system",
                    "content": (
                        "Return only a JSON object that validates against this JSON Schema. "
                        "No prose, no code fences:\n" + _canonical_json(schema)
                    ),
                }]

        prompt_sha256 = _sha256_text(_canonical_json(final_messages))
        params_json = _canonical_json(sent)
        cache_mode = getattr(self.settings, "llm_cache_mode", "live") or "live"

        # Replayability (CLAUDE.md §3 non-negotiable 8) is scoped to the
        # full NN8 cache key -- (prompt_sha256, endpoint, served_model_
        # version, params_json) -- not just the first cached response for
        # this prompt/endpoint/params (docs/specs/P6_P8_explorer_llm_
        # design.md §3.6 steps 5-7, WP N6): after a provider model upgrade,
        # an unscoped lookup could keep returning an older model's response
        # forever. Resolve the expected served version first, then look the
        # cache up by the full key.
        expected_version, ambiguous_reason = self._resolve_expected_version(
            cache_mode, endpoint, prompt_sha256, params_json,
        )
        cache_matches = (
            self.persistence.find_llm_cache(
                prompt_sha256, endpoint, params_json, served_model_version=expected_version,
            )
            if expected_version and not ambiguous_reason else []
        )
        if cache_matches:
            cached = cache_matches[0]
            # A cache hit must be behaviourally identical to the live call it
            # replays -- a structured-output caller (e.g. the Explorer plan
            # node, docs/specs/P6_P8_explorer_llm_design.md §4.8's
            # "Idempotency": a retried node attempt hits llm_cache and
            # re-derives the SAME proposal) reads `LLMResult.parsed`, never
            # `response_text` directly, so a cache hit that left `parsed`
            # unset (as this branch previously did, unconditionally) silently
            # looked like an empty/invalid response to every such caller even
            # though `status == "ok"`. Only re-parses when a schema was
            # actually given, matching `_call_live`'s own "schema is not
            # None" gate.
            cached_parsed = None
            cached_status = "ok"
            cached_error = None
            if schema is not None:
                cached_parsed, parse_err = _parse_and_validate(cached["response_text"], schema)
                if parse_err is not None:
                    cached_status = "invalid_output"
                    cached_error = f"cached response no longer validates: {parse_err}"
            return self._log_and_return(
                task=task, seq=seq, role=role, endpoint=endpoint, messages=final_messages,
                params_sent=sent, params_dropped=dropped, ctx=ctx, transport_attempt=1,
                outcome=("succeeded" if cached_status == "ok" else "invalid_output"),
                status=cached_status, source="cache",
                served_model_version=cached["served_model_version"], response_text=cached["response_text"],
                finish_reason=cached["finish_reason"], cache_hit=True, cache_key=cached["cache_key"],
                cached_from_call_id=cached["source_call_id"], prompt_sha256=prompt_sha256, params_json=params_json,
                schema=schema, prompt_template_id=prompt_template_id,
                prompt_template_version=prompt_template_version, parsed=cached_parsed,
                error_type="InvalidModelOutput" if cached_error else None, error_message=cached_error,
            )

        if cache_mode == "replay":
            # docs/specs/P6_P8_explorer_llm_design.md §3.6 step 7: replay
            # mode never makes a live call. The miss -- including an
            # ambiguous served version among several cached responses -- is
            # logged (NN7 -- even a failure to answer is a logged outcome)
            # before it is raised.
            reason = ambiguous_reason or "no cached response for this prompt/endpoint/params (LLM_CACHE_MODE=replay)"
            self._log_and_return(
                task=task, seq=seq, role=role, endpoint=endpoint, messages=final_messages,
                params_sent=sent, params_dropped=dropped, ctx=ctx, transport_attempt=1,
                outcome="replay_miss", status="unavailable", error_type="LLMReplayMiss",
                error_message=reason,
                prompt_sha256=prompt_sha256, params_json=params_json,
                prompt_template_id=prompt_template_id, prompt_template_version=prompt_template_version,
                _return=False,
            )
            raise LLMReplayMiss(endpoint, reason)

        return self._call_live(
            task=task, seq=seq, role=role, endpoint=endpoint, messages=final_messages,
            params_sent=sent, params_dropped=dropped, ctx=ctx, prompt_sha256=prompt_sha256,
            params_json=params_json, schema=schema, transport_attempt=1,
            prompt_template_id=prompt_template_id, prompt_template_version=prompt_template_version,
        )

    # ── §3.6 step 5: which served version a cache hit must match ───────────

    def _resolve_expected_version(
        self, cache_mode: str, endpoint: str, prompt_sha256: str, params_json: str,
    ) -> tuple[str | None, str | None]:
        """Returns (expected_version, ambiguous_reason). In live mode the
        expected version is simply the endpoint's last observed live
        version (§3.6 step 5, live). In replay mode it is the single
        distinct served_model_version among this prompt/endpoint/params'
        cached rows; with none, (None, None) -- a plain miss; with several,
        the latest observed live version wins if it is among them, else the
        version is ambiguous and no cache lookup or live call may proceed
        (step 5, replay)."""
        if cache_mode != "replay":
            return self.persistence.last_live_version(endpoint), None
        candidates = self.persistence.find_llm_cache(prompt_sha256, endpoint, params_json)
        versions = sorted({c["served_model_version"] for c in candidates})
        if not versions:
            return None, None
        if len(versions) == 1:
            return versions[0], None
        last_live = self.persistence.last_live_version(endpoint)
        if last_live in versions:
            return last_live, None
        return None, "ambiguous served model version among cached responses for this prompt/endpoint/params"

    # ── live call, with one transport retry and one JSON-validation retry ──

    def _call_live(
        self, *, task, seq, role, endpoint, messages, params_sent, params_dropped, ctx,
        prompt_sha256, params_json, schema, transport_attempt,
        prompt_template_id, prompt_template_version,
    ) -> LLMResult:
        common = dict(
            task=task, seq=seq, role=role, endpoint=endpoint, messages=messages,
            params_sent=params_sent, params_dropped=params_dropped, ctx=ctx,
            prompt_template_id=prompt_template_id, prompt_template_version=prompt_template_version,
        )
        try:
            resp = self.client.chat(
                endpoint=endpoint, messages=messages, params=params_sent, timeout_s=self.timeout_s,
            )
        except ModelUnavailable as exc:
            return self._log_and_return(
                **common, transport_attempt=transport_attempt, outcome="unavailable", status="unavailable",
                error_type="ModelUnavailable", error_message=str(exc), permanent=exc.permanent,
                error_status_code=exc.status_code,
            )
        except LLMConfigError as exc:
            # NN7: a 400 (a request this code built incorrectly) is a
            # configuration bug, not a transient failure -- no retry, but
            # the failed attempt must still leave a row before the same
            # exception propagates to the node (independent review
            # 2026-09-24 item 3; migration 008's outcome CHECK includes
            # 'bad_request' precisely for this path).
            self._log_and_return(
                **common, transport_attempt=transport_attempt, outcome="bad_request", status="unavailable",
                error_type="LLMConfigError", error_message=str(exc), _return=False,
                error_status_code=exc.status_code,
            )
            raise
        except (RateLimited, TransientModelError) as exc:
            if transport_attempt >= self.max_transport_attempts:
                # Give up: one row for this final attempt, outcome
                # 'unavailable' -- not also a separate 'failed_transport'
                # row for the same attempt (spec §3.6 step 8: "on the last
                # attempt, return unavailable"). `permanent=False` (perf
                # review 2026-09-25): retries were exhausted for THIS item,
                # not proof the endpoint is genuinely down -- never trips
                # `orchestrator.narration.runner`'s circuit breaker for a
                # sibling item on the same role.
                return self._log_and_return(
                    **common, transport_attempt=transport_attempt, outcome="unavailable", status="unavailable",
                    error_type=type(exc).__name__, error_message=str(exc), permanent=False,
                    error_status_code=exc.status_code,
                )
            self._log_and_return(
                **common, transport_attempt=transport_attempt, outcome="failed_transport", status="unavailable",
                error_type=type(exc).__name__, error_message=str(exc), _return=False,
                error_status_code=exc.status_code,
            )
            # Bounded exponential backoff, honouring the endpoint's own
            # `Retry-After` when it states one (rare for Databricks Model
            # Serving's own 429 body, but respected when present) rather
            # than always waiting this code's own guess; capped at
            # `retry_backoff_max_s` either way. A small random jitter
            # de-synchronises concurrent retries under `narrate`'s bounded
            # thread pool (perf review 2026-09-25) -- without it, several
            # workers that all hit the SAME rate limit at once would also
            # all retry at once, reproducing the same burst.
            retry_after = getattr(exc, "retry_after_s", None)
            if retry_after is not None:
                wait_s = min(max(retry_after, 0.0), self.retry_backoff_max_s)
            else:
                wait_s = min(self.retry_backoff_s * (2 ** (transport_attempt - 1)), self.retry_backoff_max_s)
            wait_s += random.uniform(0, min(1.0, wait_s * 0.25)) if wait_s > 0 else 0.0
            time.sleep(wait_s)
            return self._call_live(
                task=task, seq=seq, role=role, endpoint=endpoint, messages=messages,
                params_sent=params_sent, params_dropped=params_dropped, ctx=ctx,
                prompt_sha256=prompt_sha256, params_json=params_json, schema=schema,
                transport_attempt=transport_attempt + 1,
                prompt_template_id=prompt_template_id, prompt_template_version=prompt_template_version,
            )
        except TruncatedOutput as exc:
            return self._log_and_return(
                **common, transport_attempt=transport_attempt, outcome="invalid_output", status="invalid_output",
                error_type="TruncatedOutput", error_message=str(exc),
            )
        except Exception as exc:
            # NN7 (independent review 2026-09-24 item 3): an exception from
            # self.client.chat() outside the known transport-error set
            # (ModelUnavailable/LLMConfigError/RateLimited/
            # TransientModelError/TruncatedOutput, each already handled
            # above) must still leave an llm_calls row before it escapes --
            # logged the same way a first-attempt transient failure already
            # is (outcome 'failed_transport', an existing outcome in
            # migration 008's CHECK), with error_type set to the exception's
            # own class name, then re-raised unchanged so the node sees it
            # exactly as before.
            self._log_and_return(
                **common, transport_attempt=transport_attempt, outcome="failed_transport", status="unavailable",
                error_type=type(exc).__name__, error_message=str(exc), _return=False,
                error_status_code=getattr(exc, "status_code", None),
            )
            raise

        parsed = None
        if schema is not None:
            parsed, err = _parse_and_validate(resp.text, schema)
            if err is not None:
                if transport_attempt < 2:
                    self._log_and_return(
                        **common, transport_attempt=transport_attempt, outcome="invalid_output",
                        status="invalid_output", error_type="InvalidModelOutput", error_message=err,
                        served_model_version=resp.served_model_version, response_text=resp.text,
                        finish_reason=resp.finish_reason, reasoning_parts_stripped=resp.reasoning_parts_stripped,
                        prompt_tokens=resp.prompt_tokens, completion_tokens=resp.completion_tokens,
                        total_tokens=resp.total_tokens, latency_ms=resp.latency_ms, request_id=resp.request_id,
                        _return=False,
                    )
                    return self._call_live(
                        task=task, seq=seq, role=role, endpoint=endpoint, messages=messages,
                        params_sent=params_sent, params_dropped=params_dropped, ctx=ctx,
                        prompt_sha256=prompt_sha256, params_json=params_json, schema=schema,
                        transport_attempt=transport_attempt + 1,
                        prompt_template_id=prompt_template_id, prompt_template_version=prompt_template_version,
                    )
                return self._log_and_return(
                    **common, transport_attempt=transport_attempt, outcome="invalid_output",
                    status="invalid_output", error_type="InvalidModelOutput", error_message=err,
                    served_model_version=resp.served_model_version, response_text=resp.text,
                    finish_reason=resp.finish_reason, reasoning_parts_stripped=resp.reasoning_parts_stripped,
                    prompt_tokens=resp.prompt_tokens, completion_tokens=resp.completion_tokens,
                    total_tokens=resp.total_tokens, latency_ms=resp.latency_ms, request_id=resp.request_id,
                )

        cache_key = _sha256_text(
            _canonical_json([prompt_sha256, endpoint, resp.served_model_version, params_json])
        )
        return self._log_and_return(
            **common, transport_attempt=transport_attempt, outcome="succeeded", status="ok", source="live",
            served_model_version=resp.served_model_version, response_text=resp.text,
            finish_reason=resp.finish_reason, reasoning_parts_stripped=resp.reasoning_parts_stripped,
            prompt_tokens=resp.prompt_tokens, completion_tokens=resp.completion_tokens,
            total_tokens=resp.total_tokens, latency_ms=resp.latency_ms, request_id=resp.request_id,
            cache_key=cache_key, parsed=parsed, prompt_sha256=prompt_sha256, params_json=params_json,
            put_cache=True,
        )

    # ── logging (NN7: nothing returned unlogged) + cache write ─────────────

    def _log_and_return(
        self, *, task, seq, role, endpoint, messages, params_sent, params_dropped, ctx,
        transport_attempt, outcome, status, prompt_template_id, prompt_template_version,
        source=None, served_model_version=None,
        response_text=None, finish_reason=None, reasoning_parts_stripped=0,
        prompt_tokens=None, completion_tokens=None, total_tokens=None, latency_ms=None,
        request_id=None, error_type=None, error_message=None, error_status_code=None,
        cache_hit=False, cache_key=None,
        cached_from_call_id=None, prompt_sha256=None, params_json=None, schema=None,
        parsed=None, put_cache=False, _return=True, permanent=True,
    ) -> LLMResult | None:
        now = self.clock()
        version_changed = False
        if source == "live" and served_model_version:
            expected = self.persistence.last_live_version(endpoint) if endpoint else None
            version_changed = bool(expected and expected != served_model_version)

        # `role` disambiguates a fallback-role retry from the primary role's
        # own (already logged, 'unavailable') attempt for the same
        # task/seq/transport_attempt -- otherwise the two could collide on
        # `outcome` alone (e.g. both 'unavailable').
        call_id = hashlib.sha256(
            f"{ctx.execution_key or ctx.run_id or ''}|{task}|{seq}|{role}|{transport_attempt}|{source or outcome}"
            .encode("utf-8")
        ).hexdigest()[:32]

        row = {
            "call_id": call_id, "run_id": ctx.run_id, "engagement_id": ctx.engagement_id,
            "node_name": ctx.node_name, "execution_key": ctx.execution_key, "task": task, "seq": seq,
            "transport_attempt": transport_attempt, "endpoint_role": role, "endpoint": endpoint,
            "served_model_version": served_model_version, "source": source, "cache_hit": cache_hit,
            "cache_key": cache_key, "cached_from_call_id": cached_from_call_id,
            "version_changed": version_changed, "prompt_template_id": prompt_template_id,
            "prompt_template_version": prompt_template_version,
            "prompt_sha256": prompt_sha256 or _sha256_text(_canonical_json(messages)),
            "messages_json": _canonical_json(messages), "params_sent_json": _canonical_json(params_sent),
            "params_withheld_json": _canonical_json(params_dropped), "response_text": response_text,
            "reasoning_parts_stripped": reasoning_parts_stripped, "finish_reason": finish_reason,
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "total_tokens": total_tokens, "latency_ms": latency_ms, "request_id": request_id,
            "outcome": outcome, "error_type": error_type,
            "error_status_code": error_status_code, "error_message": (error_message or "")[:2000] or None,
            "pii_columns_masked_json": _canonical_json(ctx.pii_columns_masked),
            "pii_whitelist_json": _canonical_json(ctx.pii_whitelist),
            "actor": ctx.actor, "created_at": now,
        }
        try:
            self.persistence.record_llm_call(row)
        except Exception as exc:  # noqa: BLE001 -- NN7: logging failure fails the node, always
            raise LLMLoggingError(f"record_llm_call failed for call_id={call_id!r}: {exc}") from exc

        if put_cache and source == "live" and served_model_version and cache_key:
            self.persistence.put_llm_cache_if_absent({
                "cache_key": cache_key, "prompt_sha256": prompt_sha256, "endpoint": endpoint,
                "served_model_version": served_model_version, "params_json": params_json,
                "response_text": response_text or "", "finish_reason": finish_reason or "",
                "usage_json": _canonical_json({
                    "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                }),
                "source_call_id": call_id, "created_at": now,
            })

        if not _return:
            return None
        return LLMResult(
            status=status, text=response_text, parsed=parsed, call_id=call_id, source=source,
            served_model_version=served_model_version, error=error_message, permanent=permanent,
        )


def _parse_and_validate(text: str | None, schema: dict) -> tuple[dict | None, str | None]:
    if not text:
        return None, "empty response text"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"not valid JSON: {exc}"
    try:
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as exc:
        return None, f"schema validation failed: {exc.message}"
    return parsed, None
