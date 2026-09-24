"""Test doubles for orchestrator.adapters.protocols.ModelClient (independent
review 2026-09-24 item 3, docs/specs/P6_P8_explorer_llm_design.md §3.5).
Never exercised against a live endpoint -- live calls go through
DatabricksModelClient, gated behind RUN_LIVE_LLM=1 in tests/live/."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orchestrator.adapters.protocols import ModelResponse


@dataclass
class FakeModelClient:
    """`responses` maps endpoint -> a `ModelResponse`/`Exception`, or a list
    of them consumed in call order (so a test can script "fails once, then
    succeeds" for a retry scenario). `describe_responses` maps endpoint ->
    the dict describe_endpoint() should return. Every call is recorded in
    `.calls` for assertions."""

    responses: dict[str, Any] = field(default_factory=dict)
    describe_responses: dict[str, dict] = field(default_factory=dict)
    calls: list[dict] = field(default_factory=list, init=False)

    def chat(self, *, endpoint: str, messages: list[dict], params: dict, timeout_s: float) -> ModelResponse:
        self.calls.append({"endpoint": endpoint, "messages": messages, "params": params, "timeout_s": timeout_s})
        if endpoint not in self.responses:
            raise AssertionError(f"FakeModelClient: no response configured for endpoint {endpoint!r}")
        entry = self.responses[endpoint]
        if isinstance(entry, list):
            if not entry:
                raise AssertionError(f"FakeModelClient: response list exhausted for endpoint {endpoint!r}")
            item = entry.pop(0)
        else:
            item = entry
        if isinstance(item, Exception):
            raise item
        return item

    def describe_endpoint(self, endpoint: str) -> dict:
        return self.describe_responses.get(endpoint, {"foundation_model": None, "ready": True})


class RaisingModelClient:
    """Every call fails with the same exception -- for exercising a node's
    handling of a ModelClient that cannot be reached at all."""

    def __init__(self, exc: Exception):
        self._exc = exc

    def chat(self, **kwargs) -> ModelResponse:
        raise self._exc

    def describe_endpoint(self, endpoint: str) -> dict:
        raise self._exc
