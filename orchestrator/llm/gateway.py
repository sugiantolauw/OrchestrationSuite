"""LLMGateway: the one place a node calls a model (independent review
2026-09-24 item 3; docs/specs/P6_P8_explorer_llm_design.md §3.6). Every call
goes: resolve role -> filter params through the role's recorded capability
matrix -> check the response cache -> call the ModelClient (with one retry
on a rate limit / transient failure) -> validate the output against a JSON
Schema if one was given (with one client-side retry) -> log to `llm_calls`
SYNCHRONOUSLY BEFORE RETURNING (CLAUDE.md §3 non-negotiable 7) -> cache a
successful response for replay (non-negotiable 8).

A node never talks to a ModelClient directly, never chooses which
parameters to send, and never decides what "unavailable" means -- all of
that lives here, once, so every node degrades the same way (CLAUDE.md §6
NN13: "LLM unavailable — deterministic output only")."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import jsonschema

from orchestrator.llm.capabilities import filter_params, load_capabilities, role_config
from orchestrator.llm.errors import (
    LLMConfigError,
    LLMLoggingError,
    ModelUnavailable,
    RateLimited,
    TransientModelError,
    TruncatedOutput,
)


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
    ):
        self.settings = settings
        self.client = client
        self.persistence = persistence
        self.node_models = node_models
        self.capabilities = capabilities if capabilities is not None else load_capabilities()
        self.clock = clock
        self.retry_backoff_s = retry_backoff_s
        self.timeout_s = timeout_s
        self.prompt_template_id = prompt_template_id
        self.prompt_template_version = prompt_template_version

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
    ) -> LLMResult:
        role = self.node_models.get(task)
        if not role:
            raise LLMConfigError(f"no NODE_MODELS entry for task {task!r}")
        endpoint = getattr(self.settings, role, None)

        if not endpoint:
            return self._log_and_return(
                task=task, seq=seq, role=role, endpoint=None, messages=messages,
                params_sent={}, params_dropped={k: "endpoint not configured" for k in desired_params},
                ctx=ctx, transport_attempt=1, outcome="unavailable",
                error_type="LLMConfigError", error_message=f"endpoint not configured for role {role!r}",
                status="unavailable",
            )

        sent, dropped = filter_params(self.capabilities, role, desired_params)
        final_messages = list(messages)
        supports_strict = role_config(self.capabilities, role).get("params", {}).get("json_schema_strict") == "supported"
        if schema is not None:
            if supports_strict:
                sent = dict(sent)
                sent["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": task, "strict": True, "schema": schema},
                }
            else:
                dropped = dict(dropped)
                dropped["response_format"] = "untested (json_schema_strict not supported for this role)"
                final_messages = final_messages + [{
                    "role": "system",
                    "content": (
                        "Return only a JSON object that validates against this JSON Schema. "
                        "No prose, no code fences:\n" + _canonical_json(schema)
                    ),
                }]

        prompt_sha256 = _sha256_text(_canonical_json(final_messages))
        params_json = _canonical_json(sent)

        # Replayability (CLAUDE.md §3 non-negotiable 8): a prior successful
        # call with the exact same prompt/endpoint/params is served from
        # llm_cache instead of calling the endpoint again.
        cache_matches = self.persistence.find_llm_cache(prompt_sha256, endpoint, params_json)
        if cache_matches:
            cached = cache_matches[0]
            return self._log_and_return(
                task=task, seq=seq, role=role, endpoint=endpoint, messages=final_messages,
                params_sent=sent, params_dropped=dropped, ctx=ctx, transport_attempt=1,
                outcome="succeeded", status="ok", source="cache",
                served_model_version=cached["served_model_version"], response_text=cached["response_text"],
                finish_reason=cached["finish_reason"], cache_hit=True, cache_key=cached["cache_key"],
                cached_from_call_id=cached["source_call_id"], prompt_sha256=prompt_sha256, params_json=params_json,
                schema=schema,
            )

        return self._call_live(
            task=task, seq=seq, role=role, endpoint=endpoint, messages=final_messages,
            params_sent=sent, params_dropped=dropped, ctx=ctx, prompt_sha256=prompt_sha256,
            params_json=params_json, schema=schema, transport_attempt=1,
        )

    # ── live call, with one transport retry and one JSON-validation retry ──

    def _call_live(
        self, *, task, seq, role, endpoint, messages, params_sent, params_dropped, ctx,
        prompt_sha256, params_json, schema, transport_attempt,
    ) -> LLMResult:
        try:
            resp = self.client.chat(
                endpoint=endpoint, messages=messages, params=params_sent, timeout_s=self.timeout_s,
            )
        except ModelUnavailable as exc:
            return self._log_and_return(
                task=task, seq=seq, role=role, endpoint=endpoint, messages=messages,
                params_sent=params_sent, params_dropped=params_dropped, ctx=ctx,
                transport_attempt=transport_attempt, outcome="unavailable", status="unavailable",
                error_type="ModelUnavailable", error_message=str(exc),
            )
        except (RateLimited, TransientModelError) as exc:
            if transport_attempt >= 2:
                # Give up: one row for this final attempt, outcome
                # 'unavailable' -- not also a separate 'failed_transport'
                # row for the same attempt (spec §3.6 step 8: "on attempt 2,
                # return unavailable").
                return self._log_and_return(
                    task=task, seq=seq, role=role, endpoint=endpoint, messages=messages,
                    params_sent=params_sent, params_dropped=params_dropped, ctx=ctx,
                    transport_attempt=transport_attempt, outcome="unavailable", status="unavailable",
                    error_type=type(exc).__name__, error_message=str(exc),
                )
            self._log_and_return(
                task=task, seq=seq, role=role, endpoint=endpoint, messages=messages,
                params_sent=params_sent, params_dropped=params_dropped, ctx=ctx,
                transport_attempt=transport_attempt, outcome="failed_transport", status="unavailable",
                error_type=type(exc).__name__, error_message=str(exc), _return=False,
            )
            time.sleep(self.retry_backoff_s)
            return self._call_live(
                task=task, seq=seq, role=role, endpoint=endpoint, messages=messages,
                params_sent=params_sent, params_dropped=params_dropped, ctx=ctx,
                prompt_sha256=prompt_sha256, params_json=params_json, schema=schema,
                transport_attempt=transport_attempt + 1,
            )
        except TruncatedOutput as exc:
            return self._log_and_return(
                task=task, seq=seq, role=role, endpoint=endpoint, messages=messages,
                params_sent=params_sent, params_dropped=params_dropped, ctx=ctx,
                transport_attempt=transport_attempt, outcome="invalid_output", status="invalid_output",
                error_type="TruncatedOutput", error_message=str(exc),
            )

        parsed = None
        if schema is not None:
            parsed, err = _parse_and_validate(resp.text, schema)
            if err is not None:
                if transport_attempt < 2:
                    self._log_and_return(
                        task=task, seq=seq, role=role, endpoint=endpoint, messages=messages,
                        params_sent=params_sent, params_dropped=params_dropped, ctx=ctx,
                        transport_attempt=transport_attempt, outcome="invalid_output", status="invalid_output",
                        error_type="InvalidModelOutput", error_message=err,
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
                    )
                return self._log_and_return(
                    task=task, seq=seq, role=role, endpoint=endpoint, messages=messages,
                    params_sent=params_sent, params_dropped=params_dropped, ctx=ctx,
                    transport_attempt=transport_attempt, outcome="invalid_output", status="invalid_output",
                    error_type="InvalidModelOutput", error_message=err,
                    served_model_version=resp.served_model_version, response_text=resp.text,
                    finish_reason=resp.finish_reason, reasoning_parts_stripped=resp.reasoning_parts_stripped,
                    prompt_tokens=resp.prompt_tokens, completion_tokens=resp.completion_tokens,
                    total_tokens=resp.total_tokens, latency_ms=resp.latency_ms, request_id=resp.request_id,
                )

        cache_key = _sha256_text(
            _canonical_json([prompt_sha256, endpoint, resp.served_model_version, params_json])
        )
        return self._log_and_return(
            task=task, seq=seq, role=role, endpoint=endpoint, messages=messages,
            params_sent=params_sent, params_dropped=params_dropped, ctx=ctx,
            transport_attempt=transport_attempt, outcome="succeeded", status="ok", source="live",
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
        transport_attempt, outcome, status, source=None, served_model_version=None,
        response_text=None, finish_reason=None, reasoning_parts_stripped=0,
        prompt_tokens=None, completion_tokens=None, total_tokens=None, latency_ms=None,
        request_id=None, error_type=None, error_message=None, cache_hit=False, cache_key=None,
        cached_from_call_id=None, prompt_sha256=None, params_json=None, schema=None,
        parsed=None, put_cache=False, _return=True,
    ) -> LLMResult | None:
        now = self.clock()
        version_changed = False
        if source == "live" and served_model_version:
            expected = self.persistence.last_live_version(endpoint) if endpoint else None
            version_changed = bool(expected and expected != served_model_version)

        call_id = hashlib.sha256(
            f"{ctx.execution_key or ctx.run_id or ''}|{task}|{seq}|{transport_attempt}|{source or outcome}"
            .encode("utf-8")
        ).hexdigest()[:32]

        row = {
            "call_id": call_id, "run_id": ctx.run_id, "engagement_id": ctx.engagement_id,
            "node_name": ctx.node_name, "execution_key": ctx.execution_key, "task": task, "seq": seq,
            "transport_attempt": transport_attempt, "endpoint_role": role, "endpoint": endpoint,
            "served_model_version": served_model_version, "source": source, "cache_hit": cache_hit,
            "cache_key": cache_key, "cached_from_call_id": cached_from_call_id,
            "version_changed": version_changed, "prompt_template_id": self.prompt_template_id,
            "prompt_template_version": self.prompt_template_version,
            "prompt_sha256": prompt_sha256 or _sha256_text(_canonical_json(messages)),
            "messages_json": _canonical_json(messages), "params_sent_json": _canonical_json(params_sent),
            "params_withheld_json": _canonical_json(params_dropped), "response_text": response_text,
            "reasoning_parts_stripped": reasoning_parts_stripped, "finish_reason": finish_reason,
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "total_tokens": total_tokens, "latency_ms": latency_ms, "request_id": request_id,
            "outcome": outcome, "error_type": error_type,
            "error_status_code": None, "error_message": (error_message or "")[:2000] or None,
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
            served_model_version=served_model_version, error=error_message,
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
