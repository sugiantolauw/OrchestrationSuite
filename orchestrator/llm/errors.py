"""Typed errors for the LLM layer (independent review 2026-09-24 item 3;
docs/specs/P6_P8_explorer_llm_design.md §3.5-3.6). Every failure mode a
ModelClient or LLMGateway call can hit maps to exactly one of these -- never
a bare requests/openai exception, and never a silent fallback (CLAUDE.md
NN13/NN14)."""

from __future__ import annotations


class LLMConfigError(Exception):
    """A configuration bug -- an unset endpoint for a role that needs one,
    or a 400 Bad Request from the model (a request this code built
    incorrectly). Fails the node; retrying would not help."""


class ModelUnavailable(Exception):
    """CLAUDE.md §6 NN13 degraded mode: "LLM unavailable — deterministic
    output only". Raised directly for a 403 (including "rate limit of 0") or
    a 404 (no such endpoint) -- these are always `permanent=True`, since no
    amount of retrying reaches an endpoint that is disabled or does not
    exist. `LLMGateway` itself also RETURNS (never raises) an "unavailable"
    `LLMResult` when a RateLimited/TransientModelError exhausts its bounded
    retries -- that outcome carries `permanent=False` (found live
    2026-09-25, perf review: a concurrent run's transient 429 burst must not
    be treated the same as a genuinely dead endpoint). `permanent`
    distinguishes "this endpoint will not come back without operator
    action" from "could not get an answer for THIS item within the retry
    budget, might work next time" -- only the former may trip
    `orchestrator.narration.runner`'s circuit breaker for every other item
    sharing the same role."""

    def __init__(self, endpoint: str, reason: str, *, permanent: bool = True):
        self.endpoint = endpoint
        self.reason = reason
        self.permanent = permanent
        super().__init__(f"model endpoint {endpoint!r} unavailable: {reason}")


class RateLimited(Exception):
    """429. Retried by `LLMGateway` with bounded, honoured-Retry-After
    backoff (`Settings.llm_max_transport_attempts`/
    `llm_retry_backoff_max_s`) before the call counts as (non-permanent)
    unavailable -- CLAUDE.md §6 fallback rule, perf review 2026-09-25.
    `retry_after_s`, when the endpoint's own response states one, is the
    exact wait `LLMGateway` uses instead of its own computed backoff."""

    def __init__(self, endpoint: str, reason: str, *, retry_after_s: float | None = None):
        self.endpoint = endpoint
        self.reason = reason
        self.retry_after_s = retry_after_s
        super().__init__(f"model endpoint {endpoint!r} rate limited: {reason}")


class TransientModelError(Exception):
    """5xx, timeout, or connection error. Retried by `LLMGateway` the same
    bounded way `RateLimited` is (`retry_after_s` is rare for this class,
    but the field exists for a 5xx response that states one)."""

    def __init__(self, endpoint: str, reason: str, *, retry_after_s: float | None = None):
        self.endpoint = endpoint
        self.retry_after_s = retry_after_s
        self.reason = reason
        super().__init__(f"model endpoint {endpoint!r} transient failure: {reason}")


class TruncatedOutput(Exception):
    """finish_reason == "length": the model ran out of tokens before
    finishing. Never treated as a usable (if partial) response -- there is
    no text part to salvage, and a truncated JSON payload cannot be parsed
    safely."""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        super().__init__(f"model endpoint {endpoint!r}: response truncated (finish_reason=length)")


class InvalidModelOutput(Exception):
    """The response text did not parse as JSON, or failed schema validation,
    after the one permitted client-side retry (CLAUDE.md §6: "validate the
    schema client-side with one retry -- never strip markdown fences")."""

    def __init__(self, task: str, reason: str):
        self.task = task
        self.reason = reason
        super().__init__(f"task {task!r}: invalid model output: {reason}")


class LLMLoggingError(Exception):
    """Raised when persistence.record_llm_call() itself fails -- CLAUDE.md §3
    non-negotiable 7: "nothing is returned unlogged". This fails the node;
    a call whose outcome cannot be recorded is not a call this system can
    stand behind."""


class LLMReplayMiss(Exception):
    """`LLM_CACHE_MODE=replay` (docs/specs/P6_P8_explorer_llm_design.md
    §3.6 steps 5-7 / WP N6): replay mode never makes a live call. When no
    cached response exists for the exact (prompt, endpoint, params) triple,
    the gateway logs outcome='replay_miss' (NN7 -- the miss itself is
    logged, same as any other outcome) and raises this, rather than falling
    through to a live call or to a fallback role."""

    def __init__(self, endpoint: str, reason: str = "no cached response for this prompt/endpoint/params"):
        self.endpoint = endpoint
        self.reason = reason
        super().__init__(f"replay miss for endpoint {endpoint!r}: {reason}")
