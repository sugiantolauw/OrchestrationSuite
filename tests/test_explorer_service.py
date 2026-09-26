"""WP6 (docs/specs/P6_P8_explorer_llm_design.md §4.2, §4.8-§4.14, §9):
the Explorer plan node's proposal -> validate -> repair -> grey-out flow,
node-level "planner cannot emit code" enforcement, and the service-layer
functions confirm_plan/edit_explorer_plan/save_explorer_draft_skill/
publish_skill/start_explorer_run -- against a small, hand-built dataset
and a FakeModelClient (no network, CLAUDE.md §11).

Two harness styles:

- `_plan_harness()` builds a bare RunState with `profile_result` set
  directly (bypassing discover/profile) for the plan-node-focused tests
  (proposal/validate/repair/grey-out, degraded mode, cache replay) --
  faster and keeps each test's assertions about the plan node itself, not
  about profiling machinery tests/test_explorer_nodes.py already covers.
- `_build_ctx()` (ORCH_BACKEND=local, tests.test_p3_service's own pattern)
  for the full service-layer tests that need a real AppContext: confirm
  requires confirmation before execute, an edit changes the confirmed
  hash, save/publish, and a full Explorer run through the real pipeline
  producing findings on fixture data."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pandas as pd
import pytest

from orchestrator import runs as runs_module
from orchestrator import service
from orchestrator.adapters.model_fake import FakeModelClient
from orchestrator.adapters.protocols import ModelResponse
from orchestrator.config import DEFAULT_PPTX_TEMPLATE_PATH, NODE_MODELS
from orchestrator.errors import (
    ExplorerEditRejected,
    ExplorerPlanNotConfirmable,
    PromotionRequirementsNotMet,
)
from orchestrator.explorer.edit import apply_plan_edits
from orchestrator.explorer.materialise import materialise
from orchestrator.fingerprint import hash_skill_content_entries
from orchestrator.llm.errors import LLMConfigError
from orchestrator.llm.gateway import LLMGateway
from orchestrator.llm.prompts import FilePromptRepository
from orchestrator.nodes.context import NodeContext
from orchestrator.nodes.fieldwork import _plan_explorer
from orchestrator.pipeline import run_phase
from tests.conftest import canonical_ts

AUDIT_PERIOD = ("2026-01-01", "2026-02-28")
SONNET_ENDPOINT = "fake-sonnet-endpoint"
GPT_OSS_ENDPOINT = "fake-gptoss-endpoint"

MINI_SKILLS_DIR = Path(__file__).parent / "fixtures" / "skills"


# ── shared: profile fixture + a hand-built wire PlanProposal ──────────────


def _profile() -> dict:
    """One source, "expense_report", with exactly the columns
    docs/specs/P6_P8_explorer_llm_design.md §4.3's ColumnProfile shape
    describes -- built directly (not via orchestrator.explorer.profile,
    which tests/test_explorer_profile.py and test_explorer_nodes.py already
    cover) so these tests control validity precisely."""
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


def _wire_proposal(*, t1_column: str = "Amount", include_t2: bool = False) -> dict:
    """A single valid `threshold_exceedance` test/finding over "expense_report"
    (`t1_column` lets a test corrupt the column reference to exercise the
    repair round), plus an optional SECOND test/finding ("t2"/"f2") whose
    `trigger` is a code-injection payload -- schema-valid JSON (a plain
    string), semantically rejected only by V-N2's restricted expression
    compiler (orchestrator.expr.compile_expr), which is exactly the
    "planner cannot emit code" property this exercises at node level."""
    metrics_t1 = [
        {"name": "hv_count", "kind": "count", "column": None, "key": None, "unit": "count", "where": None},
        {"name": "hv_amount", "kind": "sum", "column": t1_column, "key": None, "unit": "currency", "where": None},
    ]
    tests = [{
        "key": "t1", "name": "High value claims", "primitive": "threshold_exceedance",
        "params": {
            "kind": "threshold_exceedance", "population": "p1", "column": t1_column,
            "limit": {"threshold": "hv"}, "direction": "above",
            "group_by": None, "aggregate": None, "exclude": None, "metrics": metrics_t1,
        },
        "control_key": "c1", "risk_key": "r1", "assertion": "operating",
        "control_objective": "Ensure claims stay within policy limits.",
        "risk_hypothesis": "Claims could exceed the approved limit without review.",
        "rationale": "Comparing amounts to the high value threshold surfaces claims needing review.",
    }]
    findings = [{
        "key": "f1", "test_key": "t1", "title": "High value claims", "trigger": "hv_count > 0",
        "severity": [{"when": "hv_count > 0", "then": "High"}, {"when": None, "then": "Low"}],
        "metrics_cited": ["hv_count", "hv_amount"], "thresholds_cited": ["hv"], "monetary_basis": "spend",
        "observation": "{hv_count} claims exceed the high value limit, totalling {hv_amount}.",
        "recommendation": "Review high value claims against policy.",
        "management_questions": ["What is the current review process for high value claims?"],
    }]
    if include_t2:
        tests.append({
            "key": "t2", "name": "Injection test", "primitive": "threshold_exceedance",
            "params": {
                "kind": "threshold_exceedance", "population": "p1", "column": "Amount",
                "limit": {"threshold": "hv"}, "direction": "above",
                "group_by": None, "aggregate": None, "exclude": None,
                "metrics": [
                    {"name": "mal_count", "kind": "count", "column": None, "key": None,
                     "unit": "count", "where": None},
                ],
            },
            "control_key": "c1", "risk_key": "r1", "assertion": "operating",
            "control_objective": "Ensure claims stay within policy limits.",
            "risk_hypothesis": "Claims could exceed the approved limit without review.",
            "rationale": "A second test whose finding carries a code-injection payload.",
        })
        findings.append({
            "key": "f2", "test_key": "t2", "title": "Injection finding",
            "trigger": "__import__('os').system('id')",
            "severity": [{"when": None, "then": "Low"}],
            "metrics_cited": ["mal_count"], "thresholds_cited": [], "monetary_basis": "none",
            "observation": "{mal_count} exceptions.",
            "recommendation": "No action required.",
            "management_questions": [],
        })
    return {
        "schema_version": "explorer-plan/1",
        "skill_name": "Explorer High Value Test",
        "domain": "Travel and Expense",
        "summary": "Flags claims over a high value threshold.",
        # date_column is deliberately omitted: a CSV-sourced date column
        # profiles as plain "string" (orchestrator.explorer.profile's own
        # docstring -- pandas.read_csv never infers dates), so declaring it
        # here would fail V-C2 against the REAL profile the full-pipeline
        # tests below build from expense_report.csv. Nothing in this
        # proposal needs a date_column (no between_audit_period filter, no
        # date_lag primitive).
        "sources": [{"source": "expense_report", "amount_column": "Amount",
                     "date_column": None, "entry_key": ["Employee ID"]}],
        "populations": [{"key": "p1", "source": "expense_report", "description": "All claims", "filters": []}],
        "risks": [{"key": "r1", "title": "Overspend risk", "description": "Claims may exceed policy limits."}],
        "controls": [{"key": "c1", "risk_key": "r1", "title": "Threshold review",
                      "description": "Claims are reviewed against a limit.", "type": "detective"}],
        "thresholds": [{"id": "hv", "value": 500, "unit": "currency", "description": "High value limit"}],
        "tests": tests, "findings": findings, "data_gaps": [], "assumptions": [],
    }


def _model_response(payload: dict, *, served_model_version: str) -> ModelResponse:
    return ModelResponse(
        text=json.dumps(payload), served_model_version=served_model_version, finish_reason="stop",
        prompt_tokens=10, completion_tokens=10, total_tokens=20, request_id="req-1",
        reasoning_parts_stripped=0, latency_ms=5,
    )


def _settings(**overrides):
    defaults = dict(
        catalog=None, schema=None, pptx_template_path=DEFAULT_PPTX_TEMPLATE_PATH,
        model_sonnet=SONNET_ENDPOINT, model_gpt_oss=GPT_OSS_ENDPOINT,
        llm_cache_mode="live", audit_timezone="Australia/Sydney",
        explorer_category_max_distinct=30, explorer_category_min_count=1,
        explorer_max_columns=200, pii_tag_names=(),
    )
    defaults.update(overrides)
    return type("S", (), defaults)()


def _plan_state(persistence, *, now=None) -> "RunState":  # noqa: F821 -- forward ref only in the docstring
    fp = dict(
        fingerprint_id=f"FP-{id(persistence)}", source_table_versions="{}", uploaded_file_hashes="{}",
        reference_data_hashes="{}", skill_content_hash=None, code_revision="rev1",
        dependency_lock_hash="dep1", runtime_config_hash="rc1", endpoint_config="{}",
        prompt_template_version="none", created_at=canonical_ts(0),
    )
    state = runs_module.create_run(
        persistence, run_kind="fieldwork", engagement_id="ENG-DEFAULT", skill_id=None,
        skill_version=None, mode="explorer", audit_period=AUDIT_PERIOD, objective="Assess high value claims",
        run_owner="alice", options={
            "auto_confirm_plan": False,
            "explorer": {
                "sources": [{"name": "expense_report", "kind": "local_file", "ref": "expense_report.csv",
                             "format": "csv", "file": None}],
                "reference_skill_ids": [], "audit_timezone": "Australia/Sydney",
            },
        },
        fingerprint=fp, now=now or canonical_ts(1),
    )
    data_assets = [{"source": "expense_report", "table_fqn": "expense_report.csv", "version": "v1", "kind": "local_file"}]
    return persistence.save_state(dataclasses.replace(
        state, profile_result={"kind": "explorer", "sources": _profile()}, data_assets=data_assets,
    ))


def _plan_ctx(persistence, *, client: FakeModelClient) -> NodeContext:
    llm = LLMGateway(
        settings=_settings(), client=client, persistence=persistence, node_models=NODE_MODELS,
        clock=lambda: canonical_ts(2), retry_backoff_s=0.0, timeout_s=5.0,
    )
    return NodeContext(
        settings=_settings(), persistence=persistence, data_source=object(), skill=None,
        clock=lambda: canonical_ts(2), llm=llm, prompts=FilePromptRepository(),
    )


# ── proposal -> validate -> repair -> grey-out ─────────────────────────────


def test_plan_repair_round_fixes_one_test_and_greys_another(local_persistence):
    state = _plan_state(local_persistence)
    client = FakeModelClient(responses={
        SONNET_ENDPOINT: _model_response(
            _wire_proposal(t1_column="Amountx", include_t2=True), served_model_version="sonnet-v1",
        ),
        GPT_OSS_ENDPOINT: _model_response(
            _wire_proposal(t1_column="Amount", include_t2=True), served_model_version="gpt-oss-v1",
        ),
    })
    ctx = _plan_ctx(local_persistence, client=client)

    result = _plan_explorer(ctx, state)

    plan = result.plan
    assert plan["status"] == "proposed"
    assert plan["validation_before_repair"] is not None
    assert plan["validation_before_repair"]["tests"]["t1"]["valid"] is False
    assert plan["llm"]["planner"]["outcome"] == "ok"
    assert plan["llm"]["repair"] is not None
    assert plan["llm"]["repair"]["outcome"] == "ok"

    report = plan["validation"]
    assert report["tests"]["t1"]["valid"] is True
    assert report["tests"]["t2"]["valid"] is False
    assert any(r["rule"] == "V-N2" for r in report["findings"]["f2"]["reasons"])

    # Exactly 2 calls: the first (broken) planner attempt and the repair
    # round -- never a third (§4.8's "at most one repair round ... never a
    # third call").
    assert len(client.calls) == 2
    assert "repair" in result.events[-1]["message"]


def test_plan_no_repair_needed_when_first_proposal_is_valid(local_persistence):
    state = _plan_state(local_persistence)
    client = FakeModelClient(responses={
        SONNET_ENDPOINT: _model_response(_wire_proposal(), served_model_version="sonnet-v1"),
    })
    ctx = _plan_ctx(local_persistence, client=client)

    result = _plan_explorer(ctx, state)

    assert result.plan["status"] == "proposed"
    assert result.plan["validation_before_repair"] is None
    assert result.plan["llm"]["repair"] is None
    assert result.plan["validation"]["tests"]["t1"]["valid"] is True
    assert len(client.calls) == 1  # no repair call -- GPT-OSS endpoint never touched


def test_plan_never_a_third_call_when_repair_also_fails(local_persistence):
    """A broken proposal whose repair ALSO fails validation: the report kept
    is the repair round's own (attempted) output, not silently reverted to
    the planner's -- and there is still no third call."""
    state = _plan_state(local_persistence)
    client = FakeModelClient(responses={
        SONNET_ENDPOINT: _model_response(_wire_proposal(t1_column="Amountx"), served_model_version="sonnet-v1"),
        GPT_OSS_ENDPOINT: _model_response(_wire_proposal(t1_column="StillWrong"), served_model_version="gpt-oss-v1"),
    })
    ctx = _plan_ctx(local_persistence, client=client)

    result = _plan_explorer(ctx, state)

    assert result.plan["status"] == "no_valid_tests"
    assert result.plan["validation"]["tests"]["t1"]["valid"] is False
    assert len(client.calls) == 2


def test_repair_config_error_fails_the_run_cleanly_through_the_real_pipeline(local_persistence):
    """A `plan_repair` call that raises `LLMConfigError` (orchestrator.
    llm.errors.LLMConfigError: "a request this code built incorrectly ...
    Fails the node; retrying would not help") is NOT swallowed and turned
    into a graceful "planner's proposal stands" degradation the way an
    ordinary `unavailable` repair outcome is (docs/specs/
    P6_P8_explorer_llm_design.md §3.8's "plan_repair unavailable: the
    planner's proposal stands" is for ModelUnavailable/transport failures,
    never for a 400 the code itself caused) -- it propagates out of the
    node, and CLAUDE.md's "fail loudly rather than proceed on a guess"
    applies: `orchestrator.pipeline.run_phase`'s own generic node-failure
    handling (already exercised by test_pipeline.py) must be what catches
    it, not `_plan_explorer` swallowing it. This is the decision the P8
    repair-schema fix's DO item 4 asked for, made explicit here rather than
    only implied by leaving the exception unhandled: the run ends up
    `failed`, with a reason naming the node and the error, every
    node_attempts/llm_calls row present, and the state still cleanly
    reloadable -- never a silent crash or a half-written run."""
    state = _plan_state(local_persistence)
    fingerprint = local_persistence.get_fingerprint(state.fingerprint_id)
    client = FakeModelClient(responses={
        SONNET_ENDPOINT: _model_response(_wire_proposal(t1_column="Amountx"), served_model_version="sonnet-v1"),
        GPT_OSS_ENDPOINT: LLMConfigError(
            "model endpoint 'fake-gptoss-endpoint' rejected the request (400): "
            "schema has too many properties maximum allowed is 128"
        ),
    })
    ctx = _plan_ctx(local_persistence, client=client)

    def plan_node(skill, run_state):
        return _plan_explorer(ctx, run_state)

    nodes_for = {"fieldwork": {"plan": [("plan", plan_node)], "execute": [], "export": []}}

    final = run_phase(
        local_persistence, state.run_id, nodes_for=nodes_for, skill=None,
        clock=lambda: canonical_ts(3), current_fingerprint=fingerprint,
    )

    assert final.status == "failed"
    assert "plan" in (final.status_reason or "")
    assert "LLMConfigError" in (final.status_reason or "")
    assert final.plan is None  # the node's partial work never landed in persisted state

    attempts = local_persistence.list_node_attempts(state.run_id)
    assert len(attempts) == 1
    assert attempts[0]["node_name"] == "plan"
    assert attempts[0]["outcome"] == "failed"
    assert "LLMConfigError" in attempts[0]["error_detail"]

    # NN7: the repair call's own row is logged (outcome bad_request) before
    # the exception propagates, next to the planner's own succeeded row --
    # nothing about this failure is unlogged.
    calls = local_persistence.list_llm_calls(state.run_id)
    outcomes = sorted(c["outcome"] for c in calls)
    assert outcomes == ["bad_request", "succeeded"]

    # The run is cleanly reloadable -- no corrupted/half-written state.
    reloaded = local_persistence.load_state(state.run_id)
    assert reloaded.status == "failed"
    assert reloaded.state_version == final.state_version

    # BUG-EXPLORER-PLAN-1 (independent review round 5, RUN-B68ACB9ED712): the
    # workflow-preview panel reads get_explorer_review, which must surface
    # WHY the run failed -- not just run_status="failed" with no reason
    # anywhere the auditor can see (NN14).
    review = service.get_explorer_review(ctx, state.run_id)
    assert review["run_status"] == "failed"
    assert "LLMConfigError" in (review["status_reason"] or "")


# ── planner cannot emit code (node level) ──────────────────────────────────


def test_plan_node_rejects_code_injection_in_trigger(local_persistence):
    """docs/specs/P6_P8_explorer_llm_design.md §8 gate 4: a trigger carrying
    `__import__(...)` is schema-valid JSON (a plain string) but is rejected
    by V-N2's restricted expression compiler, so the test it belongs to is
    invalid/greyed and never reaches a materialised Skill -- exercised here
    through the real plan node, not just the validator unit tests."""
    # The proposal has one invalid test (t2), so the plan node's OWN
    # "needs_repair" rule fires regardless of which test is broken -- the
    # repair round is configured with the SAME malicious payload, still
    # unfixed, to show it is rejected before AND after a repair attempt.
    state = _plan_state(local_persistence)
    client = FakeModelClient(responses={
        SONNET_ENDPOINT: _model_response(_wire_proposal(include_t2=True), served_model_version="sonnet-v1"),
        GPT_OSS_ENDPOINT: _model_response(_wire_proposal(include_t2=True), served_model_version="gpt-oss-v1"),
    })
    ctx = _plan_ctx(local_persistence, client=client)

    result = _plan_explorer(ctx, state)

    before = result.plan["validation_before_repair"]
    assert before["tests"]["t2"]["valid"] is False
    assert any(r["rule"] == "V-N2" for r in before["findings"]["f2"]["reasons"])

    report = result.plan["validation"]
    assert report["tests"]["t1"]["valid"] is True
    assert report["tests"]["t2"]["valid"] is False
    reasons = [r["rule"] for r in report["findings"]["f2"]["reasons"]]
    assert "V-N2" in reasons
    assert len(client.calls) == 2  # planner + one repair round, never a third
    # The malicious test can never be included: apply_plan_edits keeps every
    # test regardless of validity (only confirm_plan enforces "valid AND
    # included"), so the real gate is service.confirm_plan's own check.
    effective = apply_plan_edits(result.plan["proposal"], [])
    kept_keys = {t["key"] for t in effective["tests"]}
    valid_included = {k for k in kept_keys if report["tests"].get(k, {}).get("valid")}
    assert valid_included == {"t1"}


# ── degraded mode ───────────────────────────────────────────────────────────


def test_plan_degraded_when_model_unavailable(local_persistence):
    state = _plan_state(local_persistence)
    ctx = _plan_ctx(local_persistence, client=FakeModelClient(responses={}))
    ctx = dataclasses.replace(ctx, settings=_settings(model_sonnet=None))  # endpoint not configured
    ctx.llm.settings = ctx.settings  # NodeContext/LLMGateway share the same stub instance

    result = _plan_explorer(ctx, state)

    assert result.plan["status"] == "llm_unavailable"
    assert result.plan["proposal"] is None
    assert result.plan["llm"]["repair"] is None
    assert "LLM unavailable" in result.events[-1]["message"]


# ── cache replay ────────────────────────────────────────────────────────────


def test_plan_replay_is_byte_identical_and_makes_no_second_call(local_persistence):
    """§4.8 "Idempotency": a retried node attempt re-renders identical
    messages and hits llm_cache, never a second live call -- the SAME
    FakeModelClient is configured with exactly ONE response, so a second
    network call would raise."""
    state = _plan_state(local_persistence)
    client = FakeModelClient(responses={
        SONNET_ENDPOINT: [_model_response(_wire_proposal(), served_model_version="sonnet-v1")],
    })
    ctx = _plan_ctx(local_persistence, client=client)

    first = _plan_explorer(ctx, state)
    second = _plan_explorer(ctx, state)

    assert len(client.calls) == 1
    assert first.plan["proposal_sha256"] == second.plan["proposal_sha256"]
    assert first.plan["proposal"] == second.plan["proposal"]
    assert first.plan["llm"]["planner"]["source"] == "live"
    assert second.plan["llm"]["planner"]["source"] == "cache"


# ── service-layer: confirm / edit / draft / publish / full run ─────────────


def _write_expense_data(root: Path) -> None:
    df = pd.DataFrame([
        {"Employee ID": 1, "Transaction Date": "2026-01-05", "Amount": 100, "Category": "Travel", "Currency": "AUD"},
        {"Employee ID": 2, "Transaction Date": "2026-01-10", "Amount": 700, "Category": "Travel", "Currency": "AUD"},
        {"Employee ID": 3, "Transaction Date": "2026-01-15", "Amount": 50, "Category": "Meals", "Currency": "AUD"},
        {"Employee ID": 4, "Transaction Date": "2026-02-01", "Amount": 900, "Category": "Travel", "Currency": "AUD"},
        {"Employee ID": 5, "Transaction Date": "2026-02-10", "Amount": 300, "Category": "Meals", "Currency": "AUD"},
    ])
    df.to_csv(root / "expense_report.csv", index=False)


def _build_ctx(tmp_path: Path, *, model_sonnet: str | None = SONNET_ENDPOINT,
                worker_id: str = "worker-a") -> service.AppContext:
    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(tmp_path / "data"),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
        "ORCH_WORKER_ID": worker_id,
        "SKILLS_DIR": str(MINI_SKILLS_DIR),
        "CODE_REVISION": "test-fixed-revision",
        "AUDIT_TIMEZONE": "Australia/Sydney",
    }
    if model_sonnet:
        env["MODEL_SONNET"] = model_sonnet
        env["MODEL_GPT_OSS"] = GPT_OSS_ENDPOINT
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    _write_expense_data(data_dir)
    ctx = service.build_app_context(env)
    return ctx


def _start_and_plan(ctx: service.AppContext, *, client: FakeModelClient, include_t2: bool = False) -> str:
    ctx.model_client = client
    run_id = service.start_explorer_run(
        ctx, objective="Assess high value claims", sources=[{"kind": "local_file", "ref": "expense_report.csv"}],
        audit_period=AUDIT_PERIOD, run_owner="alice",
    )
    ctx.executor.start()
    try:
        _wait_for(ctx, run_id, {"awaiting_confirmation", "failed"})
    finally:
        pass
    return run_id


def _wait_for(ctx, run_id, statuses, timeout=15):
    import time

    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = service.get_run(ctx, run_id)["status"]
        if last in statuses:
            return last
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach {statuses} in time (last={last})")


def test_confirm_required_before_execute(tmp_path):
    ctx = _build_ctx(tmp_path)
    ctx.executor.start()
    try:
        client = FakeModelClient(responses={SONNET_ENDPOINT: _model_response(_wire_proposal(), served_model_version="v1")})
        ctx.model_client = client
        run_id = service.start_explorer_run(
            ctx, objective="Assess high value claims",
            sources=[{"kind": "local_file", "ref": "expense_report.csv"}],
            audit_period=AUDIT_PERIOD, run_owner="alice",
        )
        # confirm_plan before the plan node has even run: the run is still
        # queued/running in the plan phase, never awaiting_confirmation yet.
        with pytest.raises(ExplorerPlanNotConfirmable):
            service.confirm_plan(ctx, run_id, "alice")

        status = _wait_for(ctx, run_id, {"awaiting_confirmation", "failed"})
        assert status == "awaiting_confirmation", service.get_run(ctx, run_id).get("status_reason")
        assert service.get_run(ctx, run_id)["phase"] == "plan"

        service.confirm_plan(ctx, run_id, "alice")
        status = _wait_for(ctx, run_id, {"awaiting_signoff", "failed"})
        assert status == "awaiting_signoff", service.get_run(ctx, run_id).get("status_reason")
        assert service.get_run(ctx, run_id)["phase"] == "execute"
    finally:
        ctx.executor.stop()


def test_edit_changes_the_confirmed_hash(tmp_path):
    """A second, independent finding rule so excluding t2 still leaves t1
    included -- confirm_plan requires at least one valid included test."""
    ctx = _build_ctx(tmp_path)
    ctx.executor.start()
    try:
        proposal_with_t2 = _wire_proposal(include_t2=False)
        # Give t2 a VALID (non-malicious) trigger for this test -- edit
        # correctness, not the code-injection gate, is what is under test.
        wire = _wire_proposal(include_t2=True)
        wire["findings"][1]["trigger"] = "mal_count > 0"
        client = FakeModelClient(responses={SONNET_ENDPOINT: _model_response(wire, served_model_version="v1")})
        ctx.model_client = client
        run_id = service.start_explorer_run(
            ctx, objective="Assess high value claims",
            sources=[{"kind": "local_file", "ref": "expense_report.csv"}],
            audit_period=AUDIT_PERIOD, run_owner="alice",
        )
        _wait_for(ctx, run_id, {"awaiting_confirmation", "failed"})

        review = service.get_explorer_review(ctx, run_id)
        assert {t["key"] for t in review["tests"] if t["valid"]} == {"t1", "t2"}

        service.edit_explorer_plan(ctx, run_id, [{"op": "exclude_test", "test_key": "t2"}], actor="alice")

        state = ctx.persistence.load_state(run_id)
        plan = state.plan
        effective_after_edit = apply_plan_edits(plan["proposal"], state.plan_edits)
        assert {t["key"] for t in effective_after_edit["tests"]} == {"t1"}

        options = (state.options or {}).get("explorer", {})
        run_sources = [{"name": s["name"], "kind": s["kind"], "format": s.get("format"), "file": s.get("file")}
                       for s in options.get("sources", [])]
        unedited_effective = apply_plan_edits(plan["proposal"], [])
        unedited_files = materialise(
            unedited_effective, profile=(state.profile_result or {}).get("sources", {}),
            run_sources=run_sources, run_id=run_id, confirmed_at=ctx.clock(), owner="alice",
            audit_timezone="Australia/Sydney",
        )
        unedited_hash = hash_skill_content_entries(list(unedited_files.items()))

        service.confirm_plan(ctx, run_id, "alice")
        confirmed_state = ctx.persistence.load_state(run_id)
        assert confirmed_state.confirmed_plan_hash is not None
        assert confirmed_state.confirmed_plan_hash != unedited_hash
    finally:
        ctx.executor.stop()


def test_edit_batch_rejected_whole_when_it_leaves_an_included_test_invalid(tmp_path):
    ctx = _build_ctx(tmp_path)
    ctx.executor.start()
    try:
        client = FakeModelClient(responses={SONNET_ENDPOINT: _model_response(_wire_proposal(), served_model_version="v1")})
        ctx.model_client = client
        run_id = service.start_explorer_run(
            ctx, objective="Assess high value claims",
            sources=[{"kind": "local_file", "ref": "expense_report.csv"}],
            audit_period=AUDIT_PERIOD, run_owner="alice",
        )
        _wait_for(ctx, run_id, {"awaiting_confirmation", "failed"})

        before = ctx.persistence.load_state(run_id).plan_edits
        with pytest.raises(ExplorerEditRejected):
            service.edit_explorer_plan(
                ctx, run_id, [{"op": "set_column", "test_key": "t1", "param_path": "params.column",
                                "value": "NoSuchColumn"}],
                actor="alice",
            )
        after = ctx.persistence.load_state(run_id).plan_edits
        assert after == before  # nothing partial recorded
    finally:
        ctx.executor.stop()


def test_explorer_run_executes_same_pipeline_as_playbook_and_produces_findings(tmp_path):
    ctx = _build_ctx(tmp_path)
    ctx.executor.start()
    try:
        client = FakeModelClient(responses={SONNET_ENDPOINT: _model_response(_wire_proposal(), served_model_version="v1")})
        ctx.model_client = client
        run_id = service.start_explorer_run(
            ctx, objective="Assess high value claims",
            sources=[{"kind": "local_file", "ref": "expense_report.csv"}],
            audit_period=AUDIT_PERIOD, run_owner="alice",
        )
        _wait_for(ctx, run_id, {"awaiting_confirmation", "failed"})
        service.confirm_plan(ctx, run_id, "alice")
        status = _wait_for(ctx, run_id, {"awaiting_signoff", "failed"})
        assert status == "awaiting_signoff", service.get_run(ctx, run_id).get("status_reason")

        findings = ctx.persistence.list_findings(run_id)
        assert len(findings) == 1
        finding = findings[0]
        assert finding["rule_id"].startswith(f"EXPLORER-{run_id}.")
        assert finding["skill_id"] == f"EXPLORER-{run_id}"

        metrics = ctx.persistence.get_run_metrics(run_id)
        assert metrics["hv_count"]["value"] == 2  # rows 700 and 900 exceed the 500 limit
        assert metrics["hv_amount"]["value"] == 1600.0

        service.sign_off(ctx, run_id, "alice")
        status = _wait_for(ctx, run_id, {"completed", "failed"})
        assert status == "completed", service.get_run(ctx, run_id).get("status_reason")
    finally:
        ctx.executor.stop()


def test_save_draft_skill_and_publish_refused_without_reviewer(tmp_path):
    ctx = _build_ctx(tmp_path)
    ctx.executor.start()
    try:
        client = FakeModelClient(responses={SONNET_ENDPOINT: _model_response(_wire_proposal(), served_model_version="v1")})
        ctx.model_client = client
        run_id = service.start_explorer_run(
            ctx, objective="Assess high value claims",
            sources=[{"kind": "local_file", "ref": "expense_report.csv"}],
            audit_period=AUDIT_PERIOD, run_owner="alice",
        )
        _wait_for(ctx, run_id, {"awaiting_confirmation", "failed"})
        service.confirm_plan(ctx, run_id, "alice")
        _wait_for(ctx, run_id, {"awaiting_signoff", "failed"})
        service.sign_off(ctx, run_id, "alice")
        _wait_for(ctx, run_id, {"completed", "failed"})

        saved = service.save_explorer_draft_skill(ctx, run_id, "alice")
        assert saved["skill_id"].startswith("SKILL-X-")

        # Idempotent: saving the SAME completed run again returns the same
        # content_hash rather than raising SkillVersionConflict.
        saved_again = service.save_explorer_draft_skill(ctx, run_id, "alice")
        assert saved_again == saved

        with pytest.raises(PromotionRequirementsNotMet) as exc_none:
            service.publish_skill(ctx, saved["skill_id"], saved["version"], reviewer=None)
        assert any("reviewer" in m for m in exc_none.value.missing)

        with pytest.raises(PromotionRequirementsNotMet) as exc_same:
            service.publish_skill(ctx, saved["skill_id"], saved["version"], reviewer="alice")
        assert any("reviewer" in m for m in exc_same.value.missing)

        with pytest.raises(PromotionRequirementsNotMet) as exc_no_s2:
            service.publish_skill(ctx, saved["skill_id"], saved["version"], reviewer="bob")
        assert any("Surface 2" in m for m in exc_no_s2.value.missing)
    finally:
        ctx.executor.stop()


def test_explorer_run_degrades_when_model_unavailable(tmp_path):
    ctx = _build_ctx(tmp_path, model_sonnet=None)
    ctx.executor.start()
    try:
        ctx.model_client = FakeModelClient(responses={})
        run_id = service.start_explorer_run(
            ctx, objective="Assess high value claims",
            sources=[{"kind": "local_file", "ref": "expense_report.csv"}],
            audit_period=AUDIT_PERIOD, run_owner="alice",
        )
        status = _wait_for(ctx, run_id, {"awaiting_confirmation", "failed"})
        assert status == "awaiting_confirmation"
        state = ctx.persistence.load_state(run_id)
        assert state.plan["status"] == "llm_unavailable"

        with pytest.raises(ExplorerPlanNotConfirmable):
            service.confirm_plan(ctx, run_id, "alice")
    finally:
        ctx.executor.stop()
