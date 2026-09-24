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
    output only". Raised for a 403 (including "rate limit of 0"), a 404 (no
    such endpoint), or when transient failures/rate limiting exhaust their
    one retry. `permanent` distinguishes "this endpoint will not come back
    without operator action" (403/404) from "exhausted after one retry,
    might work next time" -- both are ModelUnavailable to a caller, since
    neither justifies a second automatic attempt right now."""

    def __init__(self, endpoint: str, reason: str, *, permanent: bool = True):
        self.endpoint = endpoint
        self.reason = reason
        self.permanent = permanent
        super().__init__(f"model endpoint {endpoint!r} unavailable: {reason}")


class RateLimited(Exception):
    """429. Retried once by LLMGateway; a second 429 becomes ModelUnavailable."""

    def __init__(self, endpoint: str, reason: str):
        self.endpoint = endpoint
        self.reason = reason
        super().__init__(f"model endpoint {endpoint!r} rate limited: {reason}")


class TransientModelError(Exception):
    """5xx, timeout, or connection error. Retried once by LLMGateway; a
    second failure becomes ModelUnavailable."""

    def __init__(self, endpoint: str, reason: str):
        self.endpoint = endpoint
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
