"""orchestrator.llm.gateway.LLMGateway (independent review 2026-09-24 item
3): role resolution from config, capability-filtered params, synchronous
logging to llm_calls BEFORE returning (NN7), cache-backed replay, and the
retry/degraded-mode rules from CLAUDE.md §6 NN13. Exercised against
LocalPersistence (a real, small sqlite database) and FakeModelClient/
RaisingModelClient -- no live endpoint calls."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from orchestrator.adapters.model_fake import FakeModelClient
from orchestrator.adapters.protocols import ModelResponse
from orchestrator.adapters.persistence_local import LocalPersistence
from orchestrator.llm.errors import (
    LLMConfigError,
    LLMLoggingError,
    LLMReplayMiss,
    ModelUnavailable,
    RateLimited,
    TransientModelError,
    TruncatedOutput,
)
from orchestrator.llm.gateway import CallContext, LLMGateway

_CAPS = {
    "roles": {
        "model_gpt_oss": {
            "verified_on": "2026-09-23", "served_model_prefix": "gpt-oss-120b",
            "params": {
                "max_tokens": "supported", "temperature": "supported",
                "json_schema_strict": "supported", "seed": "rejected",
            },
        },
        "model_sonnet": {
            "verified_on": "2026-09-23", "served_model_prefix": "claude-sonnet",
            "params": {"max_tokens": "supported", "temperature": "supported"},
        },
    }
}

NODE_MODELS = {"classify": "model_gpt_oss", "find": "model_sonnet"}


@dataclass
class _Settings:
    model_gpt_oss: str | None = "databricks-gpt-oss-120b"
    model_sonnet: str | None = None
    llm_cache_mode: str = "live"


def _resp(text="hello", model="gpt-oss-120b-080525", finish_reason="stop", reasoning=0):
    return ModelResponse(
        text=text, served_model_version=model, finish_reason=finish_reason,
        prompt_tokens=5, completion_tokens=3, total_tokens=8, request_id="req-1",
        reasoning_parts_stripped=reasoning, latency_ms=42,
    )


def _persistence():
    p = LocalPersistence(":memory:")
    p.migrate()
    return p


def _ctx(**overrides):
    base = dict(run_id="RUN-1", engagement_id="ENG-1", node_name="classify", execution_key="EK-1", actor="alice")
    base.update(overrides)
    return CallContext(**base)


def _gateway(client, persistence=None, **overrides):
    kwargs = dict(
        settings=_Settings(), client=client, persistence=persistence or _persistence(),
        node_models=NODE_MODELS, capabilities=_CAPS, retry_backoff_s=0,
    )
    kwargs.update(overrides)
    return LLMGateway(**kwargs)


# ── endpoint not configured ──────────────────────────────────────────────


def test_unconfigured_endpoint_returns_unavailable_and_logs_it():
    persistence = _persistence()
    gw = _gateway(FakeModelClient(), persistence=persistence)
    result = gw.call(
        task="find", seq=1, messages=[{"role": "user", "content": "hi"}],
        desired_params={"max_tokens": 100}, ctx=_ctx(node_name="find"),
    )
    assert result.status == "unavailable"
    rows = persistence.list_llm_calls("RUN-1")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "unavailable"
    assert "endpoint not configured" in rows[0]["error_message"]


def test_unknown_task_raises_config_error():
    gw = _gateway(FakeModelClient())
    with pytest.raises(LLMConfigError):
        gw.call(task="nonexistent_task", seq=1, messages=[], desired_params={}, ctx=_ctx())


# ── happy path + capability filtering ────────────────────────────────────


def test_successful_call_filters_unsupported_params_and_logs_succeeded():
    persistence = _persistence()
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": _resp()})
    gw = _gateway(client, persistence=persistence)
    result = gw.call(
        task="classify", seq=1, messages=[{"role": "user", "content": "hi"}],
        desired_params={"max_tokens": 100, "seed": 7}, ctx=_ctx(),
    )
    assert result.status == "ok"
    assert result.text == "hello"
    assert result.source == "live"

    sent_params = client.calls[0]["params"]
    assert sent_params == {"max_tokens": 100}  # seed dropped -- "rejected"

    rows = persistence.list_llm_calls("RUN-1")
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == "succeeded"
    assert row["served_model_version"] == "gpt-oss-120b-080525"
    assert row["reasoning_parts_stripped"] == 0
    import json
    assert json.loads(row["params_withheld_json"]) == {"seed": "rejected"}
    assert row["actor"] == "alice"
    assert row["run_id"] == "RUN-1"


def test_call_logged_before_return_even_though_caller_never_sees_persistence():
    """NN7: the row exists in llm_calls regardless of what the caller does
    with the LLMResult -- logging is not something a caller can opt out of."""
    persistence = _persistence()
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": _resp()})
    gw = _gateway(client, persistence=persistence)
    gw.call(task="classify", seq=1, messages=[], desired_params={}, ctx=_ctx())
    assert len(persistence.list_llm_calls("RUN-1")) == 1


# ── caching / replay ─────────────────────────────────────────────────────


def test_identical_second_call_is_served_from_cache_not_the_endpoint():
    persistence = _persistence()
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": [_resp(text="first")]})
    gw = _gateway(client, persistence=persistence)
    messages = [{"role": "user", "content": "hi"}]
    first = gw.call(task="classify", seq=1, messages=messages, desired_params={"max_tokens": 10}, ctx=_ctx())
    assert first.source == "live"
    assert first.text == "first"

    second = gw.call(task="classify", seq=2, messages=messages, desired_params={"max_tokens": 10}, ctx=_ctx())
    assert second.status == "ok"
    assert second.source == "cache"
    assert second.text == "first"
    assert len(client.calls) == 1  # the endpoint was never called a second time

    rows = persistence.list_llm_calls("RUN-1")
    assert len(rows) == 2
    assert rows[1]["cache_hit"] is True


def test_different_params_do_not_hit_the_same_cache_entry():
    persistence = _persistence()
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": [_resp(text="a"), _resp(text="b")]})
    gw = _gateway(client, persistence=persistence)
    messages = [{"role": "user", "content": "hi"}]
    r1 = gw.call(task="classify", seq=1, messages=messages, desired_params={"max_tokens": 10}, ctx=_ctx())
    r2 = gw.call(task="classify", seq=2, messages=messages, desired_params={"max_tokens": 20}, ctx=_ctx())
    assert r1.source == "live" and r2.source == "live"
    assert len(client.calls) == 2


# ── retry on rate limit / transient failure ──────────────────────────────


def test_rate_limited_once_then_succeeds():
    persistence = _persistence()
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": [RateLimited("ep", "slow down"), _resp(text="ok now")]
    })
    gw = _gateway(client, persistence=persistence)
    result = gw.call(task="classify", seq=1, messages=[], desired_params={}, ctx=_ctx())
    assert result.status == "ok"
    assert result.text == "ok now"
    assert len(client.calls) == 2
    rows = persistence.list_llm_calls("RUN-1")
    assert [r["outcome"] for r in sorted(rows, key=lambda r: r["transport_attempt"])] == [
        "failed_transport", "succeeded",
    ]


def test_transient_error_exhausts_bounded_retries_and_gives_up_as_non_permanent_unavailable():
    """Perf review 2026-09-25: the retry budget is `max_transport_attempts`
    (default 3 -- one more than the old hardcoded 2), and exhausting it is
    a NON-permanent unavailability -- `orchestrator.narration.runner`'s
    circuit breaker must not treat "this one item's retries ran out" the
    same as a genuinely dead endpoint."""
    persistence = _persistence()
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": [
            TransientModelError("ep", "boom"), TransientModelError("ep", "boom again"),
            TransientModelError("ep", "boom a third time"),
        ]
    })
    gw = _gateway(client, persistence=persistence)
    result = gw.call(task="classify", seq=1, messages=[], desired_params={}, ctx=_ctx())
    assert result.status == "unavailable"
    assert result.permanent is False
    assert len(client.calls) == 3
    rows = persistence.list_llm_calls("RUN-1")
    outcomes = sorted(r["outcome"] for r in rows)
    assert outcomes == ["failed_transport", "failed_transport", "unavailable"]


def test_transient_error_retry_budget_is_configurable():
    persistence = _persistence()
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": [TransientModelError("ep", "boom")],
    })
    gw = _gateway(client, persistence=persistence, max_transport_attempts=1)
    result = gw.call(task="classify", seq=1, messages=[], desired_params={}, ctx=_ctx())
    assert result.status == "unavailable"
    assert result.permanent is False
    assert len(client.calls) == 1  # max_transport_attempts=1 -- no retry at all


def test_rate_limited_twice_then_succeeds_within_the_default_budget():
    persistence = _persistence()
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": [
            RateLimited("ep", "slow down"), RateLimited("ep", "still slow"), _resp(text="ok now"),
        ]
    })
    gw = _gateway(client, persistence=persistence)
    result = gw.call(task="classify", seq=1, messages=[], desired_params={}, ctx=_ctx())
    assert result.status == "ok"
    assert result.text == "ok now"
    assert len(client.calls) == 3


def test_a_permanent_model_unavailable_is_never_downgraded_to_non_permanent():
    persistence = _persistence()
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": ModelUnavailable("ep", "rate limit of 0", permanent=True),
    })
    gw = _gateway(client, persistence=persistence)
    result = gw.call(task="classify", seq=1, messages=[], desired_params={}, ctx=_ctx())
    assert result.status == "unavailable"
    assert result.permanent is True


def test_retry_after_is_honoured_over_the_computed_backoff(monkeypatch):
    persistence = _persistence()
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": [
            RateLimited("ep", "slow down", retry_after_s=2.5), _resp(text="ok now"),
        ]
    })
    slept: list[float] = []
    monkeypatch.setattr("orchestrator.llm.gateway.time.sleep", lambda s: slept.append(s))
    monkeypatch.setattr("orchestrator.llm.gateway.random.uniform", lambda a, b: 0.0)  # deterministic
    gw = _gateway(client, persistence=persistence, retry_backoff_s=100.0)  # would dominate if NOT honoured
    result = gw.call(task="classify", seq=1, messages=[], desired_params={}, ctx=_ctx())
    assert result.status == "ok"
    assert slept == [2.5]


def test_backoff_without_retry_after_is_capped_at_retry_backoff_max_s(monkeypatch):
    persistence = _persistence()
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": [RateLimited("ep", "slow down"), _resp(text="ok now")],
    })
    slept: list[float] = []
    monkeypatch.setattr("orchestrator.llm.gateway.time.sleep", lambda s: slept.append(s))
    monkeypatch.setattr("orchestrator.llm.gateway.random.uniform", lambda a, b: 0.0)
    gw = _gateway(client, persistence=persistence, retry_backoff_s=1000.0, retry_backoff_max_s=5.0)
    gw.call(task="classify", seq=1, messages=[], desired_params={}, ctx=_ctx())
    assert slept == [5.0]


# ── round-4 narration-content fix (task item 2): the new Settings defaults
# (5 attempts, 75s cap -- orchestrator/config.py) must give enough total
# backoff to outlast a per-minute rate-limit window (~60s), while a
# genuinely dead (permanent) endpoint still fails on its first attempt
# regardless of the larger budget. ──────────────────────────────────────────


def test_new_default_retry_budget_survives_a_per_minute_rate_limit_window(monkeypatch):
    """4 consecutive RateLimited failures (no Retry-After) then success,
    under exactly the new Settings defaults (llm_max_transport_attempts=5,
    llm_retry_backoff_s=5.0, llm_retry_backoff_max_s=75.0 --
    orchestrator/config.py). The pre-round-4 defaults (3 attempts, 30s cap)
    gave only 5+10=15s of total backoff; these give 5+10+20+40=75s -- past
    the ~60s a Databricks Model Serving per-minute limit needs to clear."""
    persistence = _persistence()
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": [
            RateLimited("ep", "boom 1"), RateLimited("ep", "boom 2"),
            RateLimited("ep", "boom 3"), RateLimited("ep", "boom 4"),
            _resp(text="ok on the 5th attempt"),
        ]
    })
    slept: list[float] = []
    monkeypatch.setattr("orchestrator.llm.gateway.time.sleep", lambda s: slept.append(s))
    monkeypatch.setattr("orchestrator.llm.gateway.random.uniform", lambda a, b: 0.0)  # isolate the base schedule
    gw = _gateway(
        client, persistence=persistence,
        max_transport_attempts=5, retry_backoff_s=5.0, retry_backoff_max_s=75.0,
    )
    result = gw.call(task="classify", seq=1, messages=[], desired_params={}, ctx=_ctx())
    assert result.status == "ok"
    assert result.text == "ok on the 5th attempt"
    assert len(client.calls) == 5
    assert slept == [5.0, 10.0, 20.0, 40.0]
    assert sum(slept) == 75.0
    # The 4th wait alone starts only after 5+10+20=35s have already elapsed,
    # and the 5th (final) attempt begins only after all 75s have elapsed --
    # comfortably past the ~60s a per-minute quota needs to reset.
    assert sum(slept[:3]) < 60.0 <= sum(slept)


def test_new_default_retry_budget_still_gives_up_as_non_permanent_after_5_attempts(monkeypatch):
    persistence = _persistence()
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": [
            RateLimited("ep", f"boom {i}") for i in range(5)
        ]
    })
    monkeypatch.setattr("orchestrator.llm.gateway.time.sleep", lambda s: None)
    monkeypatch.setattr("orchestrator.llm.gateway.random.uniform", lambda a, b: 0.0)
    gw = _gateway(
        client, persistence=persistence,
        max_transport_attempts=5, retry_backoff_s=5.0, retry_backoff_max_s=75.0,
    )
    result = gw.call(task="classify", seq=1, messages=[], desired_params={}, ctx=_ctx())
    assert result.status == "unavailable"
    assert result.permanent is False  # never trips the circuit breaker for a sibling item
    assert len(client.calls) == 5


def test_a_genuinely_dead_endpoint_still_fails_fast_under_the_larger_retry_budget(monkeypatch):
    """`ModelUnavailable(permanent=True)` -- a 403/disabled/no-such-endpoint
    -- never enters the RateLimited/TransientModelError backoff loop at
    all, so raising `max_transport_attempts` to 5 and
    `retry_backoff_max_s` to 75s changes nothing about how fast this
    fails: one call, no sleep."""
    persistence = _persistence()
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": ModelUnavailable("ep", "rate limit of 0", permanent=True),
    })
    slept: list[float] = []
    monkeypatch.setattr("orchestrator.llm.gateway.time.sleep", lambda s: slept.append(s))
    gw = _gateway(
        client, persistence=persistence,
        max_transport_attempts=5, retry_backoff_s=5.0, retry_backoff_max_s=75.0,
    )
    result = gw.call(task="classify", seq=1, messages=[], desired_params={}, ctx=_ctx())
    assert result.status == "unavailable"
    assert result.permanent is True
    assert len(client.calls) == 1
    assert slept == []


# ── error_status_code: HTTP status carried onto llm_calls (quality review
# 2026-09-25 -- this column was always logged null, even for a real HTTP
# error, because the status was read to CHOOSE a typed exception and then
# discarded rather than attached to it) ─────────────────────────────────────


def test_error_status_code_is_logged_for_a_429_that_exhausts_its_retries():
    persistence = _persistence()
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": [RateLimited("ep", "slow down"), RateLimited("ep", "still slow")],
    })
    gw = _gateway(client, persistence=persistence, max_transport_attempts=2)
    gw.call(task="classify", seq=1, messages=[], desired_params={}, ctx=_ctx())
    rows = persistence.list_llm_calls("RUN-1")
    assert [r["error_status_code"] for r in sorted(rows, key=lambda r: r["transport_attempt"])] == [429, 429]


def test_error_status_code_is_logged_for_a_500():
    persistence = _persistence()
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": TransientModelError("ep", "server blew up", status_code=500),
    })
    gw = _gateway(client, persistence=persistence, max_transport_attempts=1)
    gw.call(task="classify", seq=1, messages=[], desired_params={}, ctx=_ctx())
    rows = persistence.list_llm_calls("RUN-1")
    assert rows[0]["error_status_code"] == 500


def test_error_status_code_is_logged_for_a_permanent_403():
    persistence = _persistence()
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": ModelUnavailable("ep", "nope", permanent=True, status_code=403),
    })
    gw = _gateway(client, persistence=persistence)
    gw.call(task="classify", seq=1, messages=[], desired_params={}, ctx=_ctx())
    rows = persistence.list_llm_calls("RUN-1")
    assert rows[0]["error_status_code"] == 403


def test_error_status_code_is_logged_for_a_400_bad_request():
    persistence = _persistence()
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": LLMConfigError("bad request", status_code=400),
    })
    gw = _gateway(client, persistence=persistence)
    with pytest.raises(LLMConfigError):
        gw.call(task="classify", seq=1, messages=[], desired_params={}, ctx=_ctx())
    rows = persistence.list_llm_calls("RUN-1")
    assert rows[0]["error_status_code"] == 400


def test_error_status_code_stays_none_when_there_is_no_http_status():
    """A timeout/connection error (openai's APITimeoutError/
    APIConnectionError shape) carries no status_code at all -- logging
    `None` here is honest, never a fabricated status."""
    persistence = _persistence()
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": [TransientModelError("ep", "connection reset")],
    })
    gw = _gateway(client, persistence=persistence, max_transport_attempts=1)
    gw.call(task="classify", seq=1, messages=[], desired_params={}, ctx=_ctx())
    rows = persistence.list_llm_calls("RUN-1")
    assert rows[0]["error_status_code"] is None


def test_error_status_code_stays_none_for_an_unconfigured_endpoint():
    persistence = _persistence()
    gw = _gateway(FakeModelClient(), persistence=persistence)
    gw.call(task="find", seq=1, messages=[], desired_params={}, ctx=_ctx(node_name="find"))
    rows = persistence.list_llm_calls("RUN-1")
    assert rows[0]["error_status_code"] is None


# ── schema validation with one retry ─────────────────────────────────────


SCHEMA = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"], "additionalProperties": False}


def test_valid_json_matching_schema_is_parsed():
    persistence = _persistence()
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": [_resp(text='{"x": 1}')]})
    gw = _gateway(client, persistence=persistence)
    result = gw.call(task="classify", seq=1, messages=[], desired_params={}, schema=SCHEMA, ctx=_ctx())
    assert result.status == "ok"
    assert result.parsed == {"x": 1}
    # json_schema_strict supported -- response_format was sent, not appended to the prompt
    assert "response_format" in client.calls[0]["params"]


def test_invalid_json_retries_once_then_succeeds():
    persistence = _persistence()
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": [_resp(text="not json at all"), _resp(text='{"x": 2}')]
    })
    gw = _gateway(client, persistence=persistence)
    result = gw.call(task="classify", seq=1, messages=[], desired_params={}, schema=SCHEMA, ctx=_ctx())
    assert result.status == "ok"
    assert result.parsed == {"x": 2}
    assert len(client.calls) == 2
    rows = persistence.list_llm_calls("RUN-1")
    outcomes = sorted(r["outcome"] for r in rows)
    assert outcomes == ["invalid_output", "succeeded"]


def test_invalid_json_twice_gives_up_as_invalid_output():
    persistence = _persistence()
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": [_resp(text="nope"), _resp(text="still nope")]
    })
    gw = _gateway(client, persistence=persistence)
    result = gw.call(task="classify", seq=1, messages=[], desired_params={}, schema=SCHEMA, ctx=_ctx())
    assert result.status == "invalid_output"
    assert result.parsed is None
    assert len(client.calls) == 2


def test_schema_violating_json_is_also_invalid_output():
    persistence = _persistence()
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": [_resp(text='{"wrong_field": 1}'), _resp(text='{"wrong_field": 1}')]
    })
    gw = _gateway(client, persistence=persistence)
    result = gw.call(task="classify", seq=1, messages=[], desired_params={}, schema=SCHEMA, ctx=_ctx())
    assert result.status == "invalid_output"


def test_schema_retry_false_skips_the_blind_retry():
    """Explorer perf review 2026-09-26 (BUG item 2): plan_explorer passes
    schema_retry=False because it has its OWN downstream, feedback-informed
    repair round (orchestrator.nodes.fieldwork._plan_explorer's plan_repair
    call) -- the gateway's generic blind, same-messages retry on a schema
    violation is pure wasted latency for that caller (observed live
    reproducing the identical violation on both attempts). With
    schema_retry=False, one invalid response gives up immediately -- a
    single call, not two -- while a task that never passes it (the
    default) keeps the existing one-retry behaviour unchanged."""
    persistence = _persistence()
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": [_resp(text="not json at all"), _resp(text='{"x": 2}')]
    })
    gw = _gateway(client, persistence=persistence)
    result = gw.call(
        task="classify", seq=1, messages=[], desired_params={}, schema=SCHEMA, ctx=_ctx(),
        schema_retry=False,
    )
    assert result.status == "invalid_output"
    assert len(client.calls) == 1  # no second, blind attempt
    rows = persistence.list_llm_calls("RUN-1")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "invalid_output"


# ── BUG-EXPLORER-2 (independent review round 2): actionable anyOf messages ──
# PLAN_PROPOSAL_SCHEMA's tests[].params is exactly the kind of anyOf-over-8-
# branches shape whose raw jsonschema message is a useless dump -- these
# fixtures are the two real, live-captured shapes from RUN-18A4C2D9AA8D's
# planner output that wasted its one permitted repair round on that dump.


def _split_detection_test(params: dict) -> dict:
    return {
        "schema_version": "explorer-plan/1", "skill_name": "x", "domain": "x", "summary": "x",
        "sources": [], "populations": [], "risks": [], "controls": [], "thresholds": [],
        "tests": [{
            "key": "test_split_expenses", "name": "x", "primitive": "split_detection", "params": params,
            "control_key": "c1", "risk_key": "r1", "assertion": "operating",
            "control_objective": "x", "risk_hypothesis": "x", "rationale": "x",
        }],
        "findings": [], "data_gaps": [], "assumptions": [],
    }


def test_anyof_error_names_the_one_specific_missing_property_when_kind_matches():
    import json as _json

    from orchestrator.explorer.wire_schema import PLAN_PROPOSAL_SCHEMA
    from orchestrator.llm.gateway import _parse_and_validate

    params = {
        "kind": "split_detection", "population": "pop_expense_report",
        "group_keys": ["Employee ID"], "date_column": "Transaction Date",
        "amount_column": "Expense Amount (reimbursement currency)",
        "window_days": {"threshold": "t_split_window_days"},
        "aggregate_threshold": {"threshold": "t_split_aggregate"},
        # max_line omitted -- every allow-listed param is required-but-nullable
        "metrics": [{"name": "split_excess_sum", "kind": "sum", "column": "x",
                     "key": None, "unit": "currency", "where": None}],
    }
    parsed, error = _parse_and_validate(
        _json.dumps(_split_detection_test(params)), PLAN_PROPOSAL_SCHEMA
    )
    assert parsed is None
    assert error == "schema validation failed: tests/0/params: 'max_line' is a required property"


def test_anyof_error_names_a_missing_kind_when_it_is_absent_from_every_branch():
    import json as _json

    from orchestrator.explorer.wire_schema import PLAN_PROPOSAL_SCHEMA
    from orchestrator.llm.gateway import _parse_and_validate

    params = {
        "population": "pop_expense_report", "group_keys": ["Employee ID"],
        "date_column": "Transaction Date", "amount_column": "Expense Amount (reimbursement currency)",
        "window_days": {"threshold": "t_split_window_days"},
        "aggregate_threshold": {"threshold": "t_split_aggregate"}, "max_line": None,
        "metrics": [{"name": "split_excess_sum", "kind": "sum", "column": "x",
                     "key": None, "unit": "currency", "where": None}],
    }
    parsed, error = _parse_and_validate(
        _json.dumps(_split_detection_test(params)), PLAN_PROPOSAL_SCHEMA
    )
    assert parsed is None
    assert "missing the required property 'kind'" in error
    assert "not valid under any of the given schemas" not in error


# ── permanent unavailability (403 etc) ───────────────────────────────────


def test_model_unavailable_never_retried():
    from orchestrator.llm.errors import ModelUnavailable

    persistence = _persistence()
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": [ModelUnavailable("ep", "rate limit of 0"), _resp()]
    })
    gw = _gateway(client, persistence=persistence)
    result = gw.call(task="classify", seq=1, messages=[], desired_params={}, ctx=_ctx())
    assert result.status == "unavailable"
    assert len(client.calls) == 1  # no retry -- the second canned response is never consumed


# ── bad request (400) -- NN7 fix, independent review 2026-09-24 item 3:
# migration 008's llm_calls outcome CHECK includes 'bad_request', but
# LLMConfigError from the model client used to propagate out of
# `_call_live` unlogged, leaving that outcome unreachable and the failed
# call with no trace. Each exception path out of `_call_live` must log
# before the exception (if any) propagates. ─────────────────────────────


def test_bad_request_logs_outcome_and_reraises_llm_config_error():
    persistence = _persistence()
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": [
            LLMConfigError("model endpoint 'databricks-gpt-oss-120b' rejected the request (400): bad schema"),
        ]
    })
    gw = _gateway(client, persistence=persistence)
    with pytest.raises(LLMConfigError):
        gw.call(task="classify", seq=1, messages=[{"role": "user", "content": "hi"}],
                 desired_params={"max_tokens": 10}, ctx=_ctx())
    assert len(client.calls) == 1  # a config bug is never retried

    rows = persistence.list_llm_calls("RUN-1")
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == "bad_request"
    assert row["error_type"] == "LLMConfigError"
    assert "bad schema" in row["error_message"]
    assert row["endpoint"] == "databricks-gpt-oss-120b"
    assert row["node_name"] == "classify"
    assert row["run_id"] == "RUN-1"


def test_unclassified_exception_logs_failed_transport_before_reraising():
    # Independent review 2026-09-24 item 3 (NN7): an exception from the
    # ModelClient outside the known transport-error set (ModelUnavailable/
    # LLMConfigError/RateLimited/TransientModelError/TruncatedOutput) must
    # still leave an llm_calls row -- logged as 'failed_transport' with
    # error_type the exception's own class name -- before it escapes
    # _call_live, never silently unlogged.
    persistence = _persistence()
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": [KeyError("boom")]})
    gw = _gateway(client, persistence=persistence)
    with pytest.raises(KeyError):
        gw.call(task="classify", seq=1, messages=[{"role": "user", "content": "hi"}],
                 desired_params={"max_tokens": 10}, ctx=_ctx())
    assert len(client.calls) == 1  # not retried -- unclassified, not a known transient error

    rows = persistence.list_llm_calls("RUN-1")
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == "failed_transport"
    assert row["error_type"] == "KeyError"
    assert "boom" in row["error_message"]
    assert row["endpoint"] == "databricks-gpt-oss-120b"
    assert row["node_name"] == "classify"
    assert row["run_id"] == "RUN-1"


def test_truncated_output_already_logs_invalid_output_without_raising():
    # Audit finding, not a fix: TruncatedOutput is caught in `_call_live`
    # and turned into a normal (logged) LLMResult -- it never propagates as
    # an exception, so it was already safe against NN7 before this change.
    persistence = _persistence()
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": [TruncatedOutput("databricks-gpt-oss-120b")]})
    gw = _gateway(client, persistence=persistence)
    result = gw.call(task="classify", seq=1, messages=[], desired_params={}, ctx=_ctx())
    assert result.status == "invalid_output"

    rows = persistence.list_llm_calls("RUN-1")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "invalid_output"
    assert rows[0]["error_type"] == "TruncatedOutput"


# ── logging failure fails the call ───────────────────────────────────────


class _RaisingPersistence:
    def record_llm_call(self, row):
        raise RuntimeError("db is down")

    def find_llm_cache(self, *a, **kw):
        return []

    def last_live_version(self, endpoint):
        return None


def test_logging_failure_raises_llm_logging_error():
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": [_resp()]})
    gw = _gateway(client, persistence=_RaisingPersistence())
    with pytest.raises(LLMLoggingError):
        gw.call(task="classify", seq=1, messages=[], desired_params={}, ctx=_ctx())


# ── LLM_CACHE_MODE=replay (WP N6) ─────────────────────────────────────────


def test_replay_mode_serves_a_prior_cached_response_without_calling_the_endpoint():
    persistence = _persistence()
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": [_resp(text="captured live")]})
    live_gw = _gateway(client, persistence=persistence, settings=_Settings(llm_cache_mode="live"))
    messages = [{"role": "user", "content": "hi"}]
    first = live_gw.call(task="classify", seq=1, messages=messages, desired_params={"max_tokens": 10}, ctx=_ctx())
    assert first.source == "live"

    replay_gw = _gateway(client, persistence=persistence, settings=_Settings(llm_cache_mode="replay"))
    second = replay_gw.call(task="classify", seq=2, messages=messages, desired_params={"max_tokens": 10}, ctx=_ctx())
    assert second.status == "ok"
    assert second.source == "cache"
    assert second.text == "captured live"
    assert len(client.calls) == 1  # the endpoint was never called a second time

    rows = persistence.list_llm_calls("RUN-1")
    assert [r["outcome"] for r in rows] == ["succeeded", "succeeded"]


def test_replay_mode_miss_raises_and_logs_replay_miss_never_calling_live():
    persistence = _persistence()
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": [_resp()]})
    gw = _gateway(client, persistence=persistence, settings=_Settings(llm_cache_mode="replay"))
    with pytest.raises(LLMReplayMiss):
        gw.call(task="classify", seq=1, messages=[{"role": "user", "content": "never cached"}],
                 desired_params={"max_tokens": 10}, ctx=_ctx())
    assert client.calls == []  # replay mode never makes a live call
    rows = persistence.list_llm_calls("RUN-1")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "replay_miss"
    assert rows[0]["source"] is None


def test_live_mode_is_unaffected_by_the_replay_addition():
    # "live" (the default) keeps today's behaviour: a miss just calls live.
    persistence = _persistence()
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": [_resp()]})
    gw = _gateway(client, persistence=persistence, settings=_Settings(llm_cache_mode="live"))
    result = gw.call(task="classify", seq=1, messages=[], desired_params={}, ctx=_ctx())
    assert result.status == "ok"
    assert result.source == "live"
    assert len(client.calls) == 1


# ── version-aware cache matching (independent review, NN8 fix) ────────────
# §3.6 steps 5-7: a cache hit must match the full NN8 key -- (prompt_sha256,
# endpoint, served_model_version, params_json) -- not just the first cached
# response for this prompt/endpoint/params, or a silent provider upgrade
# lets a replay keep returning an older model's response.


def test_live_mode_does_not_serve_a_stale_cached_response_after_a_version_change():
    persistence = _persistence()
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": [
        _resp(text="P answered by v1", model="v1"),   # 1. prompt P, first ever call -> v1
        _resp(text="Q answered by v2", model="v2"),   # 2. prompt Q reveals the provider moved to v2
        _resp(text="P answered by v2", model="v2"),   # 3. prompt P again, now expected to be v2
    ]})
    gw = _gateway(client, persistence=persistence, settings=_Settings(llm_cache_mode="live"))
    prompt_p = [{"role": "user", "content": "prompt p"}]
    prompt_q = [{"role": "user", "content": "prompt q"}]

    first_p = gw.call(task="classify", seq=1, messages=prompt_p, desired_params={"max_tokens": 10}, ctx=_ctx())
    assert first_p.source == "live"
    assert first_p.text == "P answered by v1"

    first_q = gw.call(task="classify", seq=2, messages=prompt_q, desired_params={"max_tokens": 10}, ctx=_ctx())
    assert first_q.source == "live"
    assert first_q.text == "Q answered by v2"

    # Before the fix, find_llm_cache(prompt, endpoint, params) ignored
    # served_model_version and returned the only (stale, v1) row cached for
    # prompt P. The cache lookup must now be scoped to the endpoint's
    # current expected version (v2, observed via prompt Q), find no match
    # for P at v2, and make a fresh live call -- never replaying v1.
    second_p = gw.call(task="classify", seq=3, messages=prompt_p, desired_params={"max_tokens": 10}, ctx=_ctx())
    assert second_p.source == "live"
    assert second_p.text == "P answered by v2"
    assert len(client.calls) == 3


def test_version_changed_flag_recorded_when_served_version_differs_from_expected():
    persistence = _persistence()
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": [
        _resp(text="first", model="v1"), _resp(text="second", model="v2"),
    ]})
    gw = _gateway(client, persistence=persistence, settings=_Settings(llm_cache_mode="live"))
    first = gw.call(task="classify", seq=1, messages=[{"role": "user", "content": "p"}],
                     desired_params={"max_tokens": 10}, ctx=_ctx())
    rows = persistence.list_llm_calls("RUN-1")
    assert rows[0]["version_changed"] is False  # nothing expected yet -- first call ever

    second = gw.call(task="classify", seq=2, messages=[{"role": "user", "content": "q"}],
                      desired_params={"max_tokens": 10}, ctx=_ctx())
    rows = persistence.list_llm_calls("RUN-1")
    second_row = next(r for r in rows if r["response_text"] == "second")
    assert second_row["version_changed"] is True  # expected v1 (from `first`), got v2
    assert first.source == "live" and second.source == "live"


def test_replay_mode_prefers_the_latest_observed_live_version_when_several_are_cached():
    persistence = _persistence()
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": [
        _resp(text="P answered by v1", model="v1"),
        _resp(text="Q answered by v2", model="v2"),
        _resp(text="P answered by v2", model="v2"),   # forced live because the v1 cache entry is stale
    ]})
    live_gw = _gateway(client, persistence=persistence, settings=_Settings(llm_cache_mode="live"))
    prompt_p = [{"role": "user", "content": "prompt p"}]
    prompt_q = [{"role": "user", "content": "prompt q"}]
    live_gw.call(task="classify", seq=1, messages=prompt_p, desired_params={"max_tokens": 10}, ctx=_ctx())
    live_gw.call(task="classify", seq=2, messages=prompt_q, desired_params={"max_tokens": 10}, ctx=_ctx())
    live_gw.call(task="classify", seq=3, messages=prompt_p, desired_params={"max_tokens": 10}, ctx=_ctx())
    # Prompt P now has two cached responses (v1 and v2) for the identical
    # prompt/endpoint/params. Replay must prefer the latest observed live
    # version (v2) and must never return the older v1 response.
    replay_gw = _gateway(client, persistence=persistence, settings=_Settings(llm_cache_mode="replay"))
    result = replay_gw.call(task="classify", seq=4, messages=prompt_p, desired_params={"max_tokens": 10}, ctx=_ctx())
    assert result.status == "ok"
    assert result.source == "cache"
    assert result.text == "P answered by v2"
    assert len(client.calls) == 3  # replay never calls the endpoint


def test_replay_mode_raises_ambiguous_replay_miss_when_no_cached_version_matches_the_latest_live_one():
    persistence = _persistence()
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": [
        _resp(text="P answered by v1", model="v1"),   # 1. prompt P -> v1
        _resp(text="Q answered by v2", model="v2"),   # 2. prompt Q -> v2 (last_live becomes v2)
        _resp(text="P answered by v3", model="v3"),   # 3. prompt P again (stale at v2) -> v3; P now {v1, v3}
        _resp(text="R answered by v4", model="v4"),   # 4. prompt R -> v4 (last_live becomes v4, matches neither)
    ]})
    live_gw = _gateway(client, persistence=persistence, settings=_Settings(llm_cache_mode="live"))
    prompt_p = [{"role": "user", "content": "prompt p"}]
    live_gw.call(task="classify", seq=1, messages=prompt_p, desired_params={"max_tokens": 10}, ctx=_ctx())
    live_gw.call(task="classify", seq=2, messages=[{"role": "user", "content": "prompt q"}],
                 desired_params={"max_tokens": 10}, ctx=_ctx())
    live_gw.call(task="classify", seq=3, messages=prompt_p, desired_params={"max_tokens": 10}, ctx=_ctx())
    live_gw.call(task="classify", seq=4, messages=[{"role": "user", "content": "prompt r"}],
                 desired_params={"max_tokens": 10}, ctx=_ctx())

    replay_gw = _gateway(client, persistence=persistence, settings=_Settings(llm_cache_mode="replay"))
    with pytest.raises(LLMReplayMiss):
        replay_gw.call(task="classify", seq=5, messages=prompt_p, desired_params={"max_tokens": 10}, ctx=_ctx())
    rows = persistence.list_llm_calls("RUN-1")
    replay_row = next(r for r in rows if r["outcome"] == "replay_miss")
    assert "ambiguous" in replay_row["error_message"]
    assert len(client.calls) == 4  # replay never calls the endpoint


# ── per-call prompt_template_id/version (WP N6) ───────────────────────────


def test_per_call_prompt_template_overrides_the_constructor_default():
    persistence = _persistence()
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": [_resp()]})
    gw = _gateway(
        client, persistence=persistence,
        prompt_template_id="classify/t43", prompt_template_version="1",
    )
    gw.call(
        task="classify", seq=1, messages=[], desired_params={}, ctx=_ctx(),
        prompt_template_id="narration/finding", prompt_template_version="v2",
    )
    rows = persistence.list_llm_calls("RUN-1")
    assert len(rows) == 1
    assert rows[0]["prompt_template_id"] == "narration/finding"
    assert rows[0]["prompt_template_version"] == "v2"


def test_call_without_a_per_call_template_falls_back_to_the_constructor_default():
    persistence = _persistence()
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": [_resp()]})
    gw = _gateway(
        client, persistence=persistence,
        prompt_template_id="classify/t43", prompt_template_version="1",
    )
    gw.call(task="classify", seq=1, messages=[], desired_params={}, ctx=_ctx())
    rows = persistence.list_llm_calls("RUN-1")
    assert rows[0]["prompt_template_id"] == "classify/t43"
    assert rows[0]["prompt_template_version"] == "1"


# ── FALLBACK_ROLE degrade matrix (CLAUDE.md §6 / WP N6) ───────────────────


def test_allowed_task_degrades_to_gpt_oss_when_sonnet_is_unconfigured():
    persistence = _persistence()
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": [_resp(text="from gpt-oss")]})
    gw = _gateway(
        client, persistence=persistence,
        node_models={"profile": "model_sonnet"},
        fallback_role={"profile": "model_gpt_oss"},
        settings=_Settings(model_sonnet=None, model_gpt_oss="databricks-gpt-oss-120b"),
    )
    result = gw.call(task="profile", seq=1, messages=[], desired_params={}, ctx=_ctx())
    assert result.status == "ok"
    assert result.text == "from gpt-oss"

    rows = sorted(persistence.list_llm_calls("RUN-1"), key=lambda r: r["created_at"])
    assert len(rows) == 2
    assert rows[0]["endpoint_role"] == "model_sonnet" and rows[0]["outcome"] == "unavailable"
    assert rows[1]["endpoint_role"] == "model_gpt_oss" and rows[1]["outcome"] == "succeeded"


def test_allowed_task_degrades_when_sonnet_call_itself_is_unavailable():
    persistence = _persistence()
    client = FakeModelClient(responses={
        "databricks-claude-sonnet-5": ModelUnavailable("databricks-claude-sonnet-5", "rate limit of 0"),
        "databricks-gpt-oss-120b": _resp(text="fallback answered"),
    })
    gw = _gateway(
        client, persistence=persistence,
        node_models={"act": "model_sonnet"},
        fallback_role={"act": "model_gpt_oss"},
        settings=_Settings(model_sonnet="databricks-claude-sonnet-5", model_gpt_oss="databricks-gpt-oss-120b"),
    )
    result = gw.call(task="act", seq=1, messages=[], desired_params={}, ctx=_ctx())
    assert result.status == "ok"
    assert result.text == "fallback answered"


def test_task_with_no_fallback_role_stays_unavailable():
    """`find` (and every task absent from FALLBACK_ROLE) never degrades --
    CLAUDE.md §6: "for find, exec summary and plan, show LLM unavailable"."""
    persistence = _persistence()
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": [_resp()]})
    gw = _gateway(
        client, persistence=persistence,
        node_models={"find": "model_sonnet"},
        fallback_role={},  # find is absent -- no fallback, even though gpt_oss IS configured
        settings=_Settings(model_sonnet=None, model_gpt_oss="databricks-gpt-oss-120b"),
    )
    result = gw.call(task="find", seq=1, messages=[], desired_params={}, ctx=_ctx())
    assert result.status == "unavailable"
    assert client.calls == []  # the gpt_oss endpoint was never touched
    rows = persistence.list_llm_calls("RUN-1")
    assert len(rows) == 1
    assert rows[0]["endpoint_role"] == "model_sonnet"


def test_fallback_skipped_when_it_resolves_to_the_same_endpoint_as_the_primary():
    """The development-workspace override (CLAUDE.md §6): MODEL_SONNET and
    MODEL_GPT_OSS pointing at the same endpoint means a second attempt
    would just repeat the failed call, so the gateway never makes it."""
    persistence = _persistence()
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": ModelUnavailable("databricks-gpt-oss-120b", "rate limit of 0"),
    })
    gw = _gateway(
        client, persistence=persistence,
        node_models={"profile": "model_sonnet"},
        fallback_role={"profile": "model_gpt_oss"},
        settings=_Settings(model_sonnet="databricks-gpt-oss-120b", model_gpt_oss="databricks-gpt-oss-120b"),
    )
    result = gw.call(task="profile", seq=1, messages=[], desired_params={}, ctx=_ctx())
    assert result.status == "unavailable"
    assert len(client.calls) == 1  # one failed attempt, no repeat


def test_fallback_role_default_matches_claude_md_section_6():
    from orchestrator.llm.tasks import FALLBACK_ROLE

    assert FALLBACK_ROLE == {
        "profile": "model_gpt_oss", "prioritise": "model_gpt_oss", "act": "model_gpt_oss",
    }
    # `find`, `plan_explorer` (the plan node) and every other task have no
    # fallback -- CLAUDE.md §6 "never silently degrade what an executive reads".
    for never_degrades in ("find", "export_summary", "plan_explorer"):
        assert FALLBACK_ROLE.get(never_degrades) is None


# ── NN11: reasoning is never stored, whatever its count ───────────────────


def test_reasoning_parts_stripped_count_is_logged_but_never_the_text():
    persistence = _persistence()
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": [_resp(text="the clean answer", reasoning=3)],
    })
    gw = _gateway(client, persistence=persistence)
    result = gw.call(task="classify", seq=1, messages=[], desired_params={}, ctx=_ctx())
    assert result.status == "ok"
    assert result.text == "the clean answer"  # ModelResponse.text is already reasoning-free

    rows = persistence.list_llm_calls("RUN-1")
    row = rows[0]
    assert row["reasoning_parts_stripped"] == 3
    # the count is the only reasoning-shaped thing in the row: response_text
    # is exactly ModelResponse.text, already reasoning-free (NN11 -- the
    # gateway trusts the ModelClient to have stripped it, and adds nothing).
    assert row["response_text"] == "the clean answer"
    assert "reasoning" not in row["response_text"]
