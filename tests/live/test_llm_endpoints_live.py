"""Optional live smoke test against a real Databricks Model Serving
endpoint (independent review 2026-09-24 item 3). Skipped unless RUN_LIVE_LLM=1
-- this whole module never runs by default, and this session never sets
that variable itself (CLAUDE.md operating instructions: do not touch the
real ledger/workspace from this session).

When it IS run (by a human, against a real .env-configured workspace), it
makes exactly one bare chat completion per configured endpoint and records
which parameters actually passed through -- the same exercise
`docs/specs/P6_P8_explorer_llm_design.md` §1's recorded matrix came from.
It does not update orchestrator/llm/capabilities.yaml automatically: a
human reviews the printed result and updates the matrix deliberately."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_LLM") != "1",
    reason="RUN_LIVE_LLM not set — live model endpoint calls are opt-in only",
)


def _settings():
    from orchestrator.config import load_settings

    return load_settings()


def test_configured_endpoints_answer_a_bare_completion():
    from orchestrator.adapters.model_databricks import DatabricksModelClient
    from orchestrator.llm.errors import ModelUnavailable

    settings = _settings()
    client = DatabricksModelClient()
    for role in ("model_gpt_oss", "model_sonnet"):
        endpoint = getattr(settings, role, None)
        if not endpoint:
            print(f"{role}: no endpoint configured, skipping")
            continue
        try:
            resp = client.chat(
                endpoint=endpoint,
                messages=[{"role": "user", "content": "Reply with the single word: ok"}],
                params={},
                timeout_s=30,
            )
            print(f"{role} ({endpoint}): served_model={resp.served_model_version!r} "
                  f"finish_reason={resp.finish_reason!r} text={resp.text!r}")
        except ModelUnavailable as exc:
            print(f"{role} ({endpoint}): unavailable — {exc}")


def test_describe_endpoint_is_a_cheap_metadata_call():
    from orchestrator.adapters.model_databricks import DatabricksModelClient

    settings = _settings()
    client = DatabricksModelClient()
    for role in ("model_gpt_oss", "model_sonnet"):
        endpoint = getattr(settings, role, None)
        if not endpoint:
            continue
        info = client.describe_endpoint(endpoint)
        print(f"{role} ({endpoint}): {info}")
        assert "ready" in info
