"""DatabricksModelClient: the ModelClient (orchestrator.adapters.protocols)
implementation that calls a real Databricks Model Serving endpoint, via
`WorkspaceClient().serving_endpoints.get_open_ai_client()` -- CLAUDE.md §6
mandates this client and forbids `mlflow.deployments` or a hand-built
`openai` client with a raw token.

Every error the endpoint can return is mapped to a typed exception from
orchestrator.llm.errors (independent review 2026-09-24 item 3) -- never a
bare openai/requests exception surfacing to a node. `content` that is a
list of parts (reasoning + text, the shape GPT-OSS-family endpoints return)
is reduced to its text parts only; the reasoning parts are counted, never
stored (CLAUDE.md §3 non-negotiable 11)."""

from __future__ import annotations

import time
import warnings
from typing import Any, Callable

from orchestrator.adapters.protocols import ModelResponse
from orchestrator.llm.errors import (
    LLMConfigError,
    ModelUnavailable,
    RateLimited,
    TransientModelError,
    TruncatedOutput,
)

_RATE_LIMIT_ZERO_MARKER = "rate limit of 0"


def _disable_mlflow_openai_autolog() -> None:
    """NN11 / independent review item 3: raw model reasoning must never be
    captured. mlflow's openai autologger records full request/response
    payloads (including reasoning parts) as artifacts -- explicitly
    disabled here, once, the first time a real client is built. A missing
    or older mlflow (no `mlflow.openai` autolog support) is not an error:
    there is nothing to disable."""
    try:
        import mlflow.openai

        mlflow.openai.autolog(disable=True)
    except Exception:  # noqa: BLE001 -- mlflow absent/older is fine, never fatal
        pass


class DatabricksModelClient:
    def __init__(self, workspace_client_factory: Callable[[], Any] | None = None):
        self._workspace_client_factory = workspace_client_factory
        self._cached_client = None

    def _client(self):
        if self._cached_client is None:
            _disable_mlflow_openai_autolog()
            if self._workspace_client_factory is not None:
                ws = self._workspace_client_factory()
            else:
                from databricks.sdk import WorkspaceClient

                ws = WorkspaceClient()
            # get_open_ai_client() is deprecated in some SDK versions in
            # favour of a differently-named accessor, but CLAUDE.md §6/D7
            # mandates this exact call -- the warning is suppressed, not
            # the call replaced.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                self._cached_client = ws.serving_endpoints.get_open_ai_client()
        return self._cached_client

    def chat(self, *, endpoint: str, messages: list[dict], params: dict, timeout_s: float) -> ModelResponse:
        start = time.monotonic()
        try:
            raw = self._client().chat.completions.with_raw_response.create(
                model=endpoint, messages=messages, timeout=timeout_s, **(params or {}),
            )
        except Exception as exc:  # noqa: BLE001 -- reclassified below, never re-raised bare
            _raise_typed(endpoint, exc)
            raise  # pragma: no cover -- _raise_typed always raises
        latency_ms = int((time.monotonic() - start) * 1000)

        request_id = None
        headers = getattr(raw, "headers", None)
        if headers is not None:
            try:
                request_id = headers.get("x-request-id")
            except Exception:  # noqa: BLE001 -- header access failure is not fatal
                request_id = None

        resp = raw.parse()
        data = resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content")
        text, reasoning_count = _extract_text(content)
        finish_reason = choice.get("finish_reason")

        if finish_reason == "length":
            # Independent review item 3: "finish_reason: length -> explicit
            # error" -- never a silently truncated (and possibly unparsable
            # JSON) response handed back as if it were complete.
            raise TruncatedOutput(endpoint)

        usage = data.get("usage") or {}
        return ModelResponse(
            text=text,
            served_model_version=data.get("model") or "",
            finish_reason=finish_reason,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            request_id=request_id,
            reasoning_parts_stripped=reasoning_count,
            latency_ms=latency_ms,
        )

    def describe_endpoint(self, endpoint: str) -> dict:
        if self._workspace_client_factory is not None:
            ws = self._workspace_client_factory()
        else:
            from databricks.sdk import WorkspaceClient

            ws = WorkspaceClient()
        ep = ws.serving_endpoints.get(endpoint)
        ready = False
        state = getattr(ep, "state", None)
        if state is not None:
            ready_value = getattr(state, "ready", None)
            ready = str(ready_value).upper() in ("READY", "ENDPOINTSTATEREADY_READY")
        foundation_model = None
        try:
            served = ep.config.served_entities[0]
            foundation_model = served.foundation_model.name
        except Exception:  # noqa: BLE001 -- not every endpoint config shape carries this
            foundation_model = None
        return {"foundation_model": foundation_model, "ready": ready}


def _extract_text(content) -> tuple[str, int]:
    if content is None:
        return "", 0
    if isinstance(content, str):
        return content, 0
    if isinstance(content, list):
        text_parts: list[str] = []
        reasoning_count = 0
        for part in content:
            if isinstance(part, dict):
                ptype = part.get("type")
                ptext = part.get("text")
            else:
                ptype = getattr(part, "type", None)
                ptext = getattr(part, "text", None)
            if ptype == "text":
                text_parts.append(ptext or "")
            elif ptype in ("reasoning", "thinking"):
                reasoning_count += 1
        return "".join(text_parts), reasoning_count
    return str(content), 0


def _retry_after_s(exc: Exception) -> float | None:
    """Best-effort `Retry-After` extraction (perf review 2026-09-25: a
    concurrent burst against a QPS-limited endpoint should back off exactly
    as long as the endpoint states, not a value this code guesses). openai's
    `APIStatusError` carries the raw `httpx.Response` on `.response`; other
    transports may expose `.headers` directly. Absence -- the common case,
    Databricks' own 429 body carries no such header -- is not an error:
    `LLMGateway` then falls back to its own computed backoff."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if headers is None:
        headers = getattr(exc, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get("retry-after") or headers.get("Retry-After")
    except Exception:  # noqa: BLE001 -- a headers-shaped object with a broken .get() is not fatal
        return None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _raise_typed(endpoint: str, exc: Exception) -> None:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "status", None)
    message = str(exc)
    lowered = message.lower()
    retry_after = _retry_after_s(exc)

    if status == 403 or _RATE_LIMIT_ZERO_MARKER in lowered or "permission_denied" in lowered:
        raise ModelUnavailable(endpoint, message, permanent=True) from exc
    if status == 404:
        raise ModelUnavailable(endpoint, message, permanent=True) from exc
    if status == 429:
        raise RateLimited(endpoint, message, retry_after_s=retry_after) from exc
    if status == 400:
        raise LLMConfigError(f"model endpoint {endpoint!r} rejected the request (400): {message}") from exc
    if status is not None and 500 <= status < 600:
        raise TransientModelError(endpoint, message, retry_after_s=retry_after) from exc
    # No status code at all -- a timeout or connection error (openai's
    # APITimeoutError/APIConnectionError carry no status_code) -- treated as
    # transient, retried by LLMGateway like any other transport failure.
    raise TransientModelError(endpoint, message, retry_after_s=retry_after) from exc
