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


def test_transient_error_twice_gives_up_as_unavailable():
    persistence = _persistence()
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": [
            TransientModelError("ep", "boom"), TransientModelError("ep", "boom again"),
        ]
    })
    gw = _gateway(client, persistence=persistence)
    result = gw.call(task="classify", seq=1, messages=[], desired_params={}, ctx=_ctx())
    assert result.status == "unavailable"
    assert len(client.calls) == 2
    rows = persistence.list_llm_calls("RUN-1")
    outcomes = sorted(r["outcome"] for r in rows)
    assert outcomes == ["failed_transport", "unavailable"]


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
