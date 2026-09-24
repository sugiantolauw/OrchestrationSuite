"""orchestrator.nodes.fieldwork's T4.3 row-level LLM classification
integration (independent review 2026-09-24 item 4): off by default
(Settings.enable_row_level_llm=False, the real default) AND requires the
Skill's plan.yaml to opt a test in via `not_testable.llm_classification` --
the real SKILL-001 does neither, so classify() behaves exactly as before
unless a test deliberately enables both, as these do."""

from __future__ import annotations

import dataclasses
import json

import pandas as pd
import pytest

from orchestrator.adapters.export_storage import LocalExportStorage
from orchestrator.adapters.model_fake import FakeModelClient, RaisingModelClient
from orchestrator.adapters.protocols import ModelResponse
from orchestrator.config import DEFAULT_PPTX_TEMPLATE_PATH
from orchestrator.contract import LocalFileDataSource
from orchestrator.nodes.context import NodeContext
from orchestrator.nodes.fieldwork import classify, discover, execute, profile
from orchestrator.skills import load_skill
from tests.conftest import canonical_ts
from tests.test_p3_nodes import AUDIT_PERIOD, MINI_SKILL_DIR, _fingerprint, _write_mini_data
from orchestrator import runs as runs_module


def _settings_stub(*, enable_row_level_llm=False):
    return type("S", (), {
        "catalog": None, "schema": None, "pptx_template_path": DEFAULT_PPTX_TEMPLATE_PATH,
        "enable_row_level_llm": enable_row_level_llm,
        "model_gpt_oss": "databricks-gpt-oss-120b", "model_sonnet": None,
        "llm_timeout_s": 30.0, "llm_retry_backoff_s": 0.0,
    })()


def _resp(payload: dict):
    return ModelResponse(
        text=json.dumps(payload), served_model_version="gpt-oss-120b-080525", finish_reason="stop",
        prompt_tokens=5, completion_tokens=5, total_tokens=10, request_id="r1",
        reasoning_parts_stripped=0, latency_ms=5,
    )


def _make_harness(local_persistence, tmp_path, *, enable_row_level_llm, llm_classification_cfg, model_client):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_mini_data(data_dir)

    skill = load_skill(MINI_SKILL_DIR)
    skill.validate()
    if llm_classification_cfg is not None:
        plan = dict(skill.plan)
        tests = list(plan["tests"])
        tests.append({
            "test_id": "T4.3",
            "not_testable": {
                "reason": "awaiting governance approval to send expense descriptions to a model",
                "flags": [],
                "llm_classification": llm_classification_cfg,
            },
        })
        plan["tests"] = tests
        skill = dataclasses.replace(skill, plan=plan)

    data_source = LocalFileDataSource(root_dir=data_dir, sources=skill.contract["sources"])
    source_versions = {name: data_source.resolve_version(name) for name in skill.contract["sources"]}

    fp = _fingerprint(f"FP-{tmp_path.name}", skill.content_hash)
    now = canonical_ts(1)
    state = runs_module.create_run(
        local_persistence, run_kind="fieldwork", engagement_id="ENG-DEFAULT", skill_id=skill.skill_id,
        skill_version=skill.version, mode="playbook", audit_period=AUDIT_PERIOD, objective="t",
        run_owner="alice", options={"auto_confirm_plan": True}, fingerprint=fp, now=now,
    )
    data_assets = [{"source": name, "table_fqn": name, "version": v} for name, v in source_versions.items()]
    state = local_persistence.save_state(dataclasses.replace(state, data_assets=data_assets))

    ctx = NodeContext(
        settings=_settings_stub(enable_row_level_llm=enable_row_level_llm),
        persistence=local_persistence, data_source=data_source, skill=skill,
        clock=lambda: canonical_ts(2), export_storage=LocalExportStorage(root_dir=tmp_path / "exports"),
        model_client=model_client,
    )
    return ctx, state


def _row_keys(ctx, source: str, n: int) -> list[str]:
    version = ctx.data_source.resolve_version(source)
    short = str(version)[:16]
    return [f"{short}:{i}" for i in range(1, n + 1)]


def _run_to_classify(ctx, state):
    state = discover(ctx, state)
    state = profile(ctx, state)
    state = execute(ctx, state)
    return classify(ctx, state)


def test_off_by_default_leaves_t43_not_testable_untouched(local_persistence, tmp_path):
    ctx, state = _make_harness(
        local_persistence, tmp_path, enable_row_level_llm=False,
        llm_classification_cfg={"source": "claims", "text_column": "Vendor"},
        model_client=RaisingModelClient(AssertionError("must never be called")),
    )
    result = _run_to_classify(ctx, state)
    t43 = next((e for e in result.exceptions if e["test_id"] == "T4.3"), None)
    assert t43 is not None
    assert t43["status"] == "not_testable"


def test_enabled_but_skill_has_no_llm_classification_block_stays_unaffected(local_persistence, tmp_path):
    ctx, state = _make_harness(
        local_persistence, tmp_path, enable_row_level_llm=True, llm_classification_cfg=None,
        model_client=RaisingModelClient(AssertionError("must never be called")),
    )
    result = _run_to_classify(ctx, state)
    # No T4.3 entry at all in the plain mini skill -- nothing to assert on
    # except that the run completed without ever touching the model client.
    assert any(e["node"] == "classify" for e in result.events)


def test_enabled_and_configured_runs_classification_and_persists_results(local_persistence, tmp_path):
    ctx, state = _make_harness(
        local_persistence, tmp_path, enable_row_level_llm=True,
        llm_classification_cfg={"source": "claims", "text_column": "Vendor", "confidence_threshold": 0.5},
        model_client=None,
    )
    keys = _row_keys(ctx, "claims", 5)
    payload = {"results": [
        {"row_key": k, "personal_expense": False, "confidence": 0.9, "rationale": "business"} for k in keys
    ]}
    ctx.model_client = FakeModelClient(responses={"databricks-gpt-oss-120b": [_resp(payload)]})

    result = _run_to_classify(ctx, state)
    t43 = next(e for e in result.exceptions if e["test_id"] == "T4.3")
    assert t43["status"] == "pass"  # nothing flagged personal_expense=True
    assert t43["exception_units"] == 0
    assert "classified" in t43["reason"]

    persisted = local_persistence.list_classification_results(state.run_id)
    assert len(persisted) == 5
    assert all(not p["personal_expense"] for p in persisted)


def test_flagged_rows_produce_an_exception_status(local_persistence, tmp_path):
    ctx, state = _make_harness(
        local_persistence, tmp_path, enable_row_level_llm=True,
        llm_classification_cfg={"source": "claims", "text_column": "Vendor", "confidence_threshold": 0.5},
        model_client=None,
    )
    keys = _row_keys(ctx, "claims", 5)
    results = [{"row_key": keys[0], "personal_expense": True, "confidence": 0.95, "rationale": "personal"}]
    results += [{"row_key": k, "personal_expense": False, "confidence": 0.8} for k in keys[1:]]
    ctx.model_client = FakeModelClient(responses={"databricks-gpt-oss-120b": [_resp({"results": results})]})

    result = _run_to_classify(ctx, state)
    t43 = next(e for e in result.exceptions if e["test_id"] == "T4.3")
    assert t43["status"] == "exception"
    assert t43["exception_units"] == 1


def test_llm_unavailable_degrades_to_unchanged_not_testable(local_persistence, tmp_path):
    from orchestrator.llm.errors import ModelUnavailable

    ctx, state = _make_harness(
        local_persistence, tmp_path, enable_row_level_llm=True,
        llm_classification_cfg={"source": "claims", "text_column": "Vendor"},
        model_client=None,
    )
    ctx.model_client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": [ModelUnavailable("ep", "rate limit of 0")]
    })
    result = _run_to_classify(ctx, state)
    t43 = next(e for e in result.exceptions if e["test_id"] == "T4.3")
    assert t43["status"] == "not_testable"
    assert local_persistence.list_classification_results(state.run_id) == []
