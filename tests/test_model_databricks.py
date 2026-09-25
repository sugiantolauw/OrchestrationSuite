"""DatabricksModelClient against a fake OpenAI-shaped client (independent
review 2026-09-24 item 3) -- no live endpoint calls. Covers every response
shape named in the task: a list-of-parts content (reasoning stripped, only
counted), content None, finish_reason=="length", and every HTTP-status-
shaped error mapped to its typed exception."""

from __future__ import annotations

import pytest

from orchestrator.adapters.model_databricks import DatabricksModelClient
from orchestrator.llm.errors import (
    LLMConfigError,
    ModelUnavailable,
    RateLimited,
    TransientModelError,
    TruncatedOutput,
)


class _FakeHeaders:
    def __init__(self, values: dict):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


class _FakeParsed:
    def __init__(self, data: dict):
        self._data = data

    def model_dump(self):
        return self._data


class _FakeRawResponse:
    def __init__(self, data: dict, headers: dict | None = None):
        self.headers = _FakeHeaders(headers or {"x-request-id": "req-123"})
        self._parsed = _FakeParsed(data)

    def parse(self):
        return self._parsed


def _chat_data(content, *, finish_reason="stop", model="gpt-oss-120b-080525", usage=None):
    return {
        "model": model,
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class _FakeAPIResponse:
    def __init__(self, headers: dict):
        self.headers = _FakeHeaders(headers)


class _FakeAPIError(Exception):
    def __init__(self, message, status_code=None, response_headers=None):
        super().__init__(message)
        self.status_code = status_code
        # Mirrors openai's APIStatusError shape (a raw httpx.Response on
        # `.response`) -- only set when a test actually wants a header
        # extracted, so every existing construction is unaffected.
        self.response = _FakeAPIResponse(response_headers) if response_headers is not None else None


class _FakeCreate:
    """Stands in for client.chat.completions.with_raw_response.create --
    returns a canned response or raises a canned exception."""

    def __init__(self, result):
        self._result = result
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeOpenAIClient:
    def __init__(self, create_fn):
        self.chat = _FakeChatNamespace(create_fn)


class _FakeChatNamespace:
    def __init__(self, create_fn):
        self.completions = _FakeCompletionsNamespace(create_fn)


class _FakeCompletionsNamespace:
    def __init__(self, create_fn):
        self.with_raw_response = _FakeWithRawResponse(create_fn)


class _FakeWithRawResponse:
    def __init__(self, create_fn):
        self.create = create_fn


def _client_with(result) -> tuple[DatabricksModelClient, _FakeCreate]:
    create_fn = _FakeCreate(result)
    fake_openai = _FakeOpenAIClient(create_fn)

    class _FakeServingEndpoints:
        def get_open_ai_client(self):
            return fake_openai

    class _FakeWorkspaceClient:
        def __init__(self):
            self.serving_endpoints = _FakeServingEndpoints()

    client = DatabricksModelClient(workspace_client_factory=_FakeWorkspaceClient)
    return client, create_fn


# ── content shapes ───────────────────────────────────────────────────────


def test_list_of_parts_keeps_only_text_and_counts_reasoning():
    data = _chat_data([
        {"type": "reasoning", "text": "internal thinking, never stored"},
        {"type": "text", "text": "the actual answer"},
    ])
    client, create_fn = _client_with(_FakeRawResponse(data))
    resp = client.chat(endpoint="ep1", messages=[{"role": "user", "content": "hi"}], params={}, timeout_s=30)
    assert resp.text == "the actual answer"
    assert resp.reasoning_parts_stripped == 1
    assert resp.served_model_version == "gpt-oss-120b-080525"
    assert resp.request_id == "req-123"
    assert resp.prompt_tokens == 10 and resp.completion_tokens == 5 and resp.total_tokens == 15


def test_multiple_text_parts_are_concatenated():
    data = _chat_data([{"type": "text", "text": "part one "}, {"type": "text", "text": "part two"}])
    client, _ = _client_with(_FakeRawResponse(data))
    resp = client.chat(endpoint="ep1", messages=[], params={}, timeout_s=30)
    assert resp.text == "part one part two"


def test_plain_string_content():
    data = _chat_data("just a string")
    client, _ = _client_with(_FakeRawResponse(data))
    resp = client.chat(endpoint="ep1", messages=[], params={}, timeout_s=30)
    assert resp.text == "just a string"
    assert resp.reasoning_parts_stripped == 0


def test_content_none_yields_empty_text():
    data = _chat_data(None)
    client, _ = _client_with(_FakeRawResponse(data))
    resp = client.chat(endpoint="ep1", messages=[], params={}, timeout_s=30)
    assert resp.text == ""
    assert resp.reasoning_parts_stripped == 0


# ── finish_reason ────────────────────────────────────────────────────────


def test_finish_reason_length_raises_truncated_output():
    data = _chat_data("partial...", finish_reason="length")
    client, _ = _client_with(_FakeRawResponse(data))
    with pytest.raises(TruncatedOutput):
        client.chat(endpoint="ep1", messages=[], params={}, timeout_s=30)


def test_finish_reason_stop_is_fine():
    data = _chat_data("done", finish_reason="stop")
    client, _ = _client_with(_FakeRawResponse(data))
    resp = client.chat(endpoint="ep1", messages=[], params={}, timeout_s=30)
    assert resp.finish_reason == "stop"


# ── error mapping ────────────────────────────────────────────────────────


def test_403_raises_model_unavailable_permanent():
    client, _ = _client_with(_FakeAPIError("nope", status_code=403))
    with pytest.raises(ModelUnavailable) as exc_info:
        client.chat(endpoint="ep1", messages=[], params={}, timeout_s=30)
    assert exc_info.value.permanent is True


def test_rate_limit_of_zero_message_is_model_unavailable_even_without_403_status():
    client, _ = _client_with(_FakeAPIError("Databricks-set rate limit of 0.", status_code=None))
    with pytest.raises(ModelUnavailable):
        client.chat(endpoint="ep1", messages=[], params={}, timeout_s=30)


def test_404_raises_model_unavailable():
    client, _ = _client_with(_FakeAPIError("no such endpoint", status_code=404))
    with pytest.raises(ModelUnavailable):
        client.chat(endpoint="ep1", messages=[], params={}, timeout_s=30)


def test_429_raises_rate_limited():
    client, _ = _client_with(_FakeAPIError("slow down", status_code=429))
    with pytest.raises(RateLimited):
        client.chat(endpoint="ep1", messages=[], params={}, timeout_s=30)


def test_429_with_retry_after_header_is_captured_on_the_exception():
    client, _ = _client_with(
        _FakeAPIError("slow down", status_code=429, response_headers={"retry-after": "7"})
    )
    with pytest.raises(RateLimited) as exc_info:
        client.chat(endpoint="ep1", messages=[], params={}, timeout_s=30)
    assert exc_info.value.retry_after_s == 7.0


def test_429_without_retry_after_header_leaves_it_none():
    client, _ = _client_with(_FakeAPIError("slow down", status_code=429))
    with pytest.raises(RateLimited) as exc_info:
        client.chat(endpoint="ep1", messages=[], params={}, timeout_s=30)
    assert exc_info.value.retry_after_s is None


def test_500_with_retry_after_header_is_captured_on_the_exception():
    client, _ = _client_with(
        _FakeAPIError("server blew up", status_code=500, response_headers={"Retry-After": "3.5"})
    )
    with pytest.raises(TransientModelError) as exc_info:
        client.chat(endpoint="ep1", messages=[], params={}, timeout_s=30)
    assert exc_info.value.retry_after_s == 3.5


def test_500_raises_transient_model_error():
    client, _ = _client_with(_FakeAPIError("server blew up", status_code=500))
    with pytest.raises(TransientModelError):
        client.chat(endpoint="ep1", messages=[], params={}, timeout_s=30)


def test_400_raises_llm_config_error():
    client, _ = _client_with(_FakeAPIError("bad schema", status_code=400))
    with pytest.raises(LLMConfigError):
        client.chat(endpoint="ep1", messages=[], params={}, timeout_s=30)


def test_no_status_code_treated_as_transient():
    client, _ = _client_with(_FakeAPIError("connection reset", status_code=None))
    with pytest.raises(TransientModelError):
        client.chat(endpoint="ep1", messages=[], params={}, timeout_s=30)


def test_params_are_passed_through_to_create():
    data = _chat_data("ok")
    client, create_fn = _client_with(_FakeRawResponse(data))
    client.chat(
        endpoint="databricks-gpt-oss-120b", messages=[{"role": "user", "content": "hi"}],
        params={"max_tokens": 100, "temperature": 0}, timeout_s=45,
    )
    assert create_fn.calls[0]["model"] == "databricks-gpt-oss-120b"
    assert create_fn.calls[0]["max_tokens"] == 100
    assert create_fn.calls[0]["temperature"] == 0
    assert create_fn.calls[0]["timeout"] == 45


# ── describe_endpoint ────────────────────────────────────────────────────


def test_describe_endpoint_reports_ready_and_foundation_model():
    class _State:
        ready = "READY"

    class _FoundationModel:
        name = "gpt-oss-120b"

    class _ServedEntity:
        foundation_model = _FoundationModel()

    class _EndpointConfig:
        served_entities = [_ServedEntity()]

    class _Endpoint:
        state = _State()
        config = _EndpointConfig()

    class _FakeServingEndpoints:
        def get(self, name):
            return _Endpoint()

    class _FakeWorkspaceClient:
        def __init__(self):
            self.serving_endpoints = _FakeServingEndpoints()

    client = DatabricksModelClient(workspace_client_factory=_FakeWorkspaceClient)
    result = client.describe_endpoint("databricks-gpt-oss-120b")
    assert result == {"foundation_model": "gpt-oss-120b", "ready": True}


def test_describe_endpoint_handles_missing_config_gracefully():
    class _Endpoint:
        state = None
        config = None

    class _FakeServingEndpoints:
        def get(self, name):
            return _Endpoint()

    class _FakeWorkspaceClient:
        def __init__(self):
            self.serving_endpoints = _FakeServingEndpoints()

    client = DatabricksModelClient(workspace_client_factory=_FakeWorkspaceClient)
    result = client.describe_endpoint("ep1")
    assert result == {"foundation_model": None, "ready": False}
