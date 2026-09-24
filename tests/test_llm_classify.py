"""orchestrator.llm.classify (independent review 2026-09-24 item 4): T4.3
row-level LLM classification via the model client (never `ai_query()`),
PII-safe (only row_key + text columns ever seen), batch-capped, with
per-row confidence. Exercised against FakeModelClient -- no live calls."""

from __future__ import annotations

import json

from orchestrator.adapters.model_fake import FakeModelClient
from orchestrator.adapters.persistence_local import LocalPersistence
from orchestrator.adapters.protocols import ModelResponse
from orchestrator.llm.classify import CLASSIFY_TASK, classify_rows, to_persisted_rows
from orchestrator.llm.gateway import CallContext, LLMGateway

NODE_MODELS = {"classify": "model_gpt_oss"}
CAPS = {"roles": {"model_gpt_oss": {"params": {
    "max_tokens": "supported", "temperature": "supported",
    "reasoning_effort": "supported", "json_schema_strict": "supported",
}}}}


class _Settings:
    model_gpt_oss = "databricks-gpt-oss-120b"
    model_sonnet = None


def _resp(payload: dict, model="gpt-oss-120b-080525"):
    return ModelResponse(
        text=json.dumps(payload), served_model_version=model, finish_reason="stop",
        prompt_tokens=10, completion_tokens=10, total_tokens=20, request_id="r1",
        reasoning_parts_stripped=0, latency_ms=5,
    )


def _gateway(client):
    p = LocalPersistence(":memory:")
    p.migrate()
    return LLMGateway(
        settings=_Settings(), client=client, persistence=p, node_models=NODE_MODELS,
        capabilities=CAPS, retry_backoff_s=0,
    ), p


def _ctx():
    return CallContext(run_id="RUN-1", engagement_id="ENG-1", node_name="classify", actor="alice")


def test_classify_rows_happy_path():
    rows = [{"__row_key": "k1", "purpose": "client dinner"}, {"__row_key": "k2", "purpose": "gift for spouse"}]
    payload = {"results": [
        {"row_key": "k1", "personal_expense": False, "confidence": 0.95, "rationale": "business"},
        {"row_key": "k2", "personal_expense": True, "confidence": 0.9, "rationale": "personal gift"},
    ]}
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": [_resp(payload)]})
    gw, _ = _gateway(client)
    results = classify_rows(rows, text_column="purpose", row_key_column="__row_key", gateway=gw, ctx=_ctx())
    assert len(results) == 2
    by_key = {r.row_key: r for r in results}
    assert by_key["k1"].personal_expense is False
    assert by_key["k2"].personal_expense is True
    assert by_key["k2"].confidence == 0.9


def test_classify_rows_only_sends_row_key_and_text_columns():
    """PII safety: no other column of a row (e.g. Employee Name, Amount)
    ever appears in the prompt sent to the model."""
    rows = [{"__row_key": "k1", "purpose": "conference", "Employee Name": "Alex Chen", "Amount": 5000}]
    payload = {"results": [{"row_key": "k1", "personal_expense": False, "confidence": 0.8}]}
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": [_resp(payload)]})
    gw, _ = _gateway(client)
    classify_rows(rows, text_column="purpose", row_key_column="__row_key", gateway=gw, ctx=_ctx())
    sent_messages = client.calls[0]["messages"]
    sent_text = json.dumps(sent_messages)
    assert "Alex Chen" not in sent_text
    assert "5000" not in sent_text
    assert "conference" in sent_text


def test_classify_rows_batches_are_capped():
    rows = [{"__row_key": f"k{i}", "purpose": "x"} for i in range(5)]
    payload_of = lambda keys: {"results": [{"row_key": k, "personal_expense": False, "confidence": 0.5} for k in keys]}
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": [
        _resp(payload_of(["k0", "k1"])), _resp(payload_of(["k2", "k3"])), _resp(payload_of(["k4"])),
    ]})
    gw, _ = _gateway(client)
    results = classify_rows(rows, text_column="purpose", row_key_column="__row_key", gateway=gw, ctx=_ctx(), batch_size=2)
    assert len(client.calls) == 3
    assert len(results) == 5


def test_classify_rows_unavailable_batch_contributes_nothing():
    from orchestrator.llm.errors import ModelUnavailable

    rows = [{"__row_key": "k1", "purpose": "x"}]
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": [ModelUnavailable("ep", "rate limit of 0")]})
    gw, _ = _gateway(client)
    results = classify_rows(rows, text_column="purpose", row_key_column="__row_key", gateway=gw, ctx=_ctx())
    assert results == []


def test_classify_rows_ignores_hallucinated_row_keys():
    rows = [{"__row_key": "k1", "purpose": "x"}]
    payload = {"results": [
        {"row_key": "k1", "personal_expense": False, "confidence": 0.5},
        {"row_key": "not-a-real-row", "personal_expense": True, "confidence": 0.99},
    ]}
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": [_resp(payload)]})
    gw, _ = _gateway(client)
    results = classify_rows(rows, text_column="purpose", row_key_column="__row_key", gateway=gw, ctx=_ctx())
    assert [r.row_key for r in results] == ["k1"]


def test_classify_rows_rejects_invalid_batch_size():
    import pytest

    client = FakeModelClient()
    gw, _ = _gateway(client)
    with pytest.raises(ValueError):
        classify_rows([], text_column="purpose", row_key_column="__row_key", gateway=gw, ctx=_ctx(), batch_size=0)


def test_to_persisted_rows_shape():
    rows = [{"__row_key": "k1", "purpose": "gift"}]
    payload = {"results": [{"row_key": "k1", "personal_expense": True, "confidence": 0.7, "rationale": "gift"}]}
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": [_resp(payload)]})
    gw, _ = _gateway(client)
    results = classify_rows(rows, text_column="purpose", row_key_column="__row_key", gateway=gw, ctx=_ctx())
    persisted = to_persisted_rows(results, now="2026-01-01T00:00:00Z")
    assert persisted == [{
        "row_key": "k1", "personal_expense": True, "confidence": 0.7, "rationale": "gift",
        "call_id": results[0].call_id, "created_at": "2026-01-01T00:00:00Z",
    }]
