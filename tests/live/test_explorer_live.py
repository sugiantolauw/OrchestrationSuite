"""WP7 (docs/specs/P6_P8_explorer_llm_design.md §8 item 8): one small,
real Explorer planner call against the dev workspace's configured Sonnet
role. Skipped unless RUN_LIVE_LLM=1 -- this whole module never runs by
default, and this session never sets that variable itself (the same
convention tests/live/test_llm_endpoints_live.py and
tests/live/test_narration_live.py already use).

A tiny synthetic profile (one source, five columns -- §8 item 8(b)'s own
shape) run through the real `_plan_explorer` node against a real
`DatabricksModelClient`. It passes if a validated `PlanProposal` comes
back (schema valid, logged in `llm_calls`), OR it `xfail`s, visibly, with
the verbatim `ModelUnavailable` reason when the endpoint is blocked --
CLAUDE.md §6's recorded result for this development workspace is exactly
that: MODEL_SONNET returns `403 PERMISSION_DENIED: ... rate limit of 0`
on every call, so today's expected live outcome IS the xfail branch, and
this test says so rather than hiding it."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_LLM") != "1",
    reason="RUN_LIVE_LLM not set — live model endpoint calls are opt-in only",
)


def _profile() -> dict:
    return {
        "expense_report": {
            "row_count": 5,
            "null_counts": {},
            "columns": [
                {
                    "name": "Amount", "type": "number", "null_count": 0, "distinct_count": 5,
                    "unique": False, "semantic_type": "amount", "pii": False, "pii_basis": None,
                    "min": 50.0, "max": 900.0, "negative_count": 0, "zero_count": 0,
                },
                {
                    "name": "Transaction Date", "type": "date", "null_count": 0, "distinct_count": 5,
                    "unique": False, "semantic_type": "date", "pii": False, "pii_basis": None,
                    "min": "2026-01-05", "max": "2026-02-10", "in_period_count": 5,
                },
                {
                    "name": "Category", "type": "string", "null_count": 0, "distinct_count": 2,
                    "unique": False, "semantic_type": "category", "pii": False, "pii_basis": None,
                    "values": [{"value": "Travel", "count": 3}, {"value": "Meals", "count": 2}],
                    "suppressed_values": 0,
                },
                {
                    "name": "Currency", "type": "string", "null_count": 0, "distinct_count": 1,
                    "unique": False, "semantic_type": "currency_code", "pii": False, "pii_basis": None,
                    "values": [{"value": "AUD", "count": 5}], "suppressed_values": 0,
                },
                {
                    "name": "Employee ID", "type": "integer", "null_count": 0, "distinct_count": 5,
                    "unique": True, "semantic_type": "identifier", "pii": True, "pii_basis": "heuristic",
                },
            ],
        }
    }


def test_one_real_planner_call_gives_a_valid_proposal_or_a_visible_unavailable_xfail(tmp_path):
    from orchestrator import runs as runs_module
    from orchestrator.adapters.model_databricks import DatabricksModelClient
    from orchestrator.adapters.persistence_local import LocalPersistence
    from orchestrator.config import NODE_MODELS, load_settings
    from orchestrator.llm.errors import ModelUnavailable
    from orchestrator.llm.gateway import LLMGateway
    from orchestrator.llm.prompts import FilePromptRepository
    from orchestrator.nodes.context import NodeContext
    from orchestrator.nodes.fieldwork import _plan_explorer
    from tests.conftest import canonical_ts

    settings = load_settings(os.environ)
    if not settings.model_sonnet:
        pytest.skip("MODEL_SONNET is not configured in this environment")

    persistence = LocalPersistence(str(tmp_path / "orch.db"))
    persistence.migrate()

    fp = dict(
        fingerprint_id="FP-LIVE", source_table_versions="{}", uploaded_file_hashes="{}",
        reference_data_hashes="{}", skill_content_hash=None, code_revision="rev1",
        dependency_lock_hash="dep1", runtime_config_hash="rc1", endpoint_config="{}",
        prompt_template_version="none", created_at=canonical_ts(0),
    )
    state = runs_module.create_run(
        persistence, run_kind="fieldwork", engagement_id="ENG-DEFAULT", skill_id=None, skill_version=None,
        mode="explorer", audit_period=("2026-01-01", "2026-02-28"), objective="Assess high value claims",
        run_owner="live-smoke", options={
            "auto_confirm_plan": False,
            "explorer": {
                "sources": [{"name": "expense_report", "kind": "local_file", "ref": "expense_report.csv",
                             "format": "csv", "file": None}],
                "reference_skill_ids": [], "audit_timezone": "Australia/Sydney",
            },
        },
        fingerprint=fp, now=canonical_ts(1),
    )
    data_assets = [{"source": "expense_report", "table_fqn": "expense_report.csv", "version": "v1", "kind": "local_file"}]
    import dataclasses

    state = persistence.save_state(dataclasses.replace(
        state, profile_result={"kind": "explorer", "sources": _profile()}, data_assets=data_assets,
    ))

    client = DatabricksModelClient()
    llm = LLMGateway(
        settings=settings, client=client, persistence=persistence, node_models=NODE_MODELS,
        clock=canonical_ts, retry_backoff_s=0.0, timeout_s=60.0,
    )
    ctx = NodeContext(
        settings=settings, persistence=persistence, data_source=object(), skill=None,
        clock=lambda: canonical_ts(2), llm=llm, prompts=FilePromptRepository(),
    )

    try:
        result = _plan_explorer(ctx, state)
    except ModelUnavailable as exc:
        pytest.xfail(f"MODEL_SONNET ({settings.model_sonnet}) is unavailable: {exc}")
        return

    calls = persistence.list_llm_calls(state.run_id)
    assert calls, "no llm_calls row logged for the live planner call"
    print(f"plan_explorer live call: status={result.plan['status']!r} "
          f"stage={'repair' if result.plan['llm']['repair'] else 'planner'}")

    if result.plan["status"] == "llm_unavailable":
        pytest.xfail(
            f"planner call recorded as unavailable by the gateway: "
            f"{result.plan['llm']['planner'].get('error')}"
        )
    assert result.plan["proposal"] is not None
    assert result.plan["validation"] is not None
