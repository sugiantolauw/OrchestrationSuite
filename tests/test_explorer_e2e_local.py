"""WP7 (docs/specs/P6_P8_explorer_llm_design.md §8 item 10, §9): Explorer
Mode end to end through the REAL state machine
(service.build_app_context + service.start_explorer_run + the real
ThreadExecutor -- never orchestrator.nodes.fieldwork._plan_explorer called
directly) using a recorded fake planner
(orchestrator.adapters.model_fake.FakeModelClient, reusing
tests/test_explorer_service.py's own `_wire_proposal`/`_model_response`
fixtures -- these have never been answered by a real Sonnet endpoint in
this development workspace, CLAUDE.md §6's recorded results, so they are
exactly the same kind of recorded/synthetic fixture the design's own
`recorded_planner_response.json` convention describes):

  - start -> propose -> validate -> one repair round -> exclude one test
    -> confirm -> execute -> findings -> sign off -> export -> save draft;
  - the degraded planner: LLM unavailable pauses at `awaiting_confirmation`
    with the deterministic profile only, no proposal, confirm_plan refused,
    and superseding it with a fresh objective moves the stuck run to
    `failed` with a reason naming the supersession;
  - the call budget: at most one planner call plus one repair call, never
    a third, across every plan-node execution above;
  - cache replay: a second Explorer run over the identical objective/
    sources/profile with LLM_CACHE_MODE=replay and a RaisingModelClient
    reproduces an identical proposal with zero live calls."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from orchestrator import service
from orchestrator.adapters.model_fake import FakeModelClient, RaisingModelClient
from orchestrator.errors import ExplorerPlanNotConfirmable
from orchestrator.llm.errors import ModelUnavailable
from tests.test_explorer_service import (
    AUDIT_PERIOD,
    GPT_OSS_ENDPOINT,
    MINI_SKILLS_DIR,
    SONNET_ENDPOINT,
    _model_response,
    _wire_proposal,
    _write_expense_data,
)

SOURCES = [{"kind": "local_file", "ref": "expense_report.csv"}]


def _build_ctx(
    tmp_path: Path, *, model_sonnet: str | None = SONNET_ENDPOINT, worker_id: str = "worker-a",
    narration_enabled: bool = False,
) -> service.AppContext:
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
    if narration_enabled:
        env["NARRATION_ENABLED"] = "true"
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    _write_expense_data(data_dir)
    return service.build_app_context(env)


def _wait_for(ctx, run_id, statuses, timeout=20):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = service.get_run(ctx, run_id)["status"]
        if last in statuses:
            return last
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach {statuses} in time (last={last})")


def _broken_then_fixed_client() -> FakeModelClient:
    """The planner's first (SONNET) attempt has an invalid t1 column AND a
    t2 whose trigger is a code-injection payload (§8 gate 4's own
    'planner cannot emit code' shape); the repair (GPT-OSS) attempt fixes
    both -- exactly tests/test_explorer_service.py's own
    test_plan_repair_round_fixes_one_test_and_greys_another, driven through
    the real service/executor instead of _plan_explorer directly."""
    first = _wire_proposal(t1_column="Amountx", include_t2=True)
    repaired = _wire_proposal(t1_column="Amount", include_t2=True)
    repaired["findings"][1]["trigger"] = "mal_count > 0"  # a valid, non-malicious trigger
    return FakeModelClient(responses={
        SONNET_ENDPOINT: _model_response(first, served_model_version="sonnet-v1"),
        GPT_OSS_ENDPOINT: _model_response(repaired, served_model_version="gpt-oss-v1"),
    })


def test_explorer_e2e_propose_repair_exclude_confirm_execute_signoff_export_save(tmp_path):
    ctx = _build_ctx(tmp_path)
    ctx.executor.start()
    try:
        client = _broken_then_fixed_client()
        ctx.model_client = client
        run_id = service.start_explorer_run(
            ctx, objective="Assess high value claims", sources=SOURCES,
            audit_period=AUDIT_PERIOD, run_owner="alice",
        )
        status = _wait_for(ctx, run_id, {"awaiting_confirmation", "failed"})
        assert status == "awaiting_confirmation", service.get_run(ctx, run_id).get("status_reason")

        state = ctx.persistence.load_state(run_id)
        plan = state.plan
        assert plan["validation_before_repair"] is not None, "the repair round never ran"
        assert plan["llm"]["repair"] is not None and plan["llm"]["repair"]["outcome"] == "ok"

        review = service.get_explorer_review(ctx, run_id)
        assert {t["key"] for t in review["tests"] if t["valid"]} == {"t1", "t2"}

        # exclude one of the two valid tests
        service.edit_explorer_plan(ctx, run_id, [{"op": "exclude_test", "test_key": "t2"}], actor="alice")

        service.confirm_plan(ctx, run_id, "alice")
        status = _wait_for(ctx, run_id, {"awaiting_signoff", "failed"})
        assert status == "awaiting_signoff", service.get_run(ctx, run_id).get("status_reason")

        findings = ctx.persistence.list_findings(run_id)
        assert len(findings) == 1
        assert findings[0]["rule_id"].startswith(f"EXPLORER-{run_id}.")
        assert findings[0]["skill_id"] == f"EXPLORER-{run_id}"

        service.sign_off(ctx, run_id, "alice")
        status = _wait_for(ctx, run_id, {"completed", "failed"})
        assert status == "completed", service.get_run(ctx, run_id).get("status_reason")

        pptx_filename, pptx_bytes = service.get_export(ctx, run_id, "pptx")
        xlsx_filename, xlsx_bytes = service.get_export(ctx, run_id, "xlsx")
        assert len(pptx_bytes) > 1000
        assert len(xlsx_bytes) > 1000

        saved = service.save_explorer_draft_skill(ctx, run_id, "alice")
        assert saved["skill_id"].startswith("SKILL-X-")
        assert any(s["skill_id"] == saved["skill_id"] for s in service.list_skills(ctx))

        # ── call budget: at most one planner call, one repair, never a
        #    third -- and NN7 every one is logged ──────────────────────────
        assert len(client.calls) == 2, "planner + repair should be exactly 2 calls, never a third"
        calls = ctx.persistence.list_llm_calls(run_id)
        seqs = sorted({c["seq"] for c in calls})
        assert seqs and max(seqs) <= 2, seqs
        for c in calls:
            assert c["messages_json"]
            assert c["endpoint"] in (SONNET_ENDPOINT, GPT_OSS_ENDPOINT)
            assert c["served_model_version"]
    finally:
        ctx.executor.stop()


def test_explorer_e2e_narration_enabled_survives_the_explorer_shaped_profile_result(tmp_path):
    """Live regression (independent review 2026-09-25): orchestrator.narration.
    payloads.build_profile_payload assumed `state.profile_result` always has
    the Playbook shape (`{source: {row_count, null_counts}}`), but an
    Explorer run's profile_result is one level deeper -- `{"kind":
    "explorer", "sources": {source: {row_count, null_counts, columns}}}`
    (orchestrator.nodes.fieldwork._profile_explorer). Iterating that dict
    directly yielded `("kind", "explorer")` as a (source, info) pair and
    crashed every Explorer run with NARRATION_ENABLED=true with
    `AttributeError: 'str' object has no attribute 'get'` inside the real
    `narrate` node. None of this file's other tests caught it because none
    of them enable narration -- this one drives the real state machine
    (service.start_explorer_run + the real ThreadExecutor, never
    orchestrator.nodes.narration.narrate called directly) with
    NARRATION_ENABLED=true, so a regression here fails the same way a real
    Explorer run would."""
    ctx = _build_ctx(tmp_path, narration_enabled=True)
    ctx.executor.start()
    try:
        client = FakeModelClient(responses={
            SONNET_ENDPOINT: _model_response(_wire_proposal(), served_model_version="sonnet-v1"),
            # narrate's "captions" job is the one narration task routed to
            # GPT-OSS (NODE_MODELS); this response is schema-invalid for it
            # (it's a PlanProposal, not a chart-captions/1 payload) on
            # purpose -- narrate's own repair-then-fallback path (§4.1,
            # orchestrator.narration.runner._generate_item) is designed to
            # degrade to template text on bad content, never raise, so this
            # test's only real assertion is that the OLD crash (a bare
            # AttributeError out of build_profile_payload, well before any
            # model call) is gone.
            GPT_OSS_ENDPOINT: _model_response(_wire_proposal(), served_model_version="gpt-oss-v1"),
        })
        ctx.model_client = client
        run_id = service.start_explorer_run(
            ctx, objective="Assess high value claims", sources=SOURCES,
            audit_period=AUDIT_PERIOD, run_owner="alice",
        )
        status = _wait_for(ctx, run_id, {"awaiting_confirmation", "failed"})
        assert status == "awaiting_confirmation", service.get_run(ctx, run_id).get("status_reason")

        service.confirm_plan(ctx, run_id, "alice")
        status = _wait_for(ctx, run_id, {"awaiting_signoff", "failed"})
        reason = service.get_run(ctx, run_id).get("status_reason")
        assert "object has no attribute" not in (reason or ""), reason
        assert status == "awaiting_signoff", reason

        state = ctx.persistence.load_state(run_id)
        assert state.profile_result.get("kind") == "explorer"
        assert "expense_report" in state.profile_result.get("sources", {})

        # the narrate node actually ran (not skipped/short-circuited) and
        # wrote a profile narrative row -- proof build_profile_payload was
        # exercised over the real Explorer-shaped profile_result, not just
        # that the run happened to avoid calling it.
        narratives = ctx.persistence.get_narratives(run_id)
        profile_rows = [n for n in narratives if n["target_kind"] == "profile"]
        assert profile_rows, narratives

        # build_profile_payload itself, called directly over this run's real
        # Explorer profile_result: the row count for the one bound source
        # comes through, never a crash and never a fabricated 0/None.
        from orchestrator.narration.payloads import build_profile_payload

        payload, table = build_profile_payload(state)
        assert table["rows_expense_report"].value == state.profile_result["sources"]["expense_report"]["row_count"]
        assert table["rows_expense_report"].value is not None

        service.sign_off(ctx, run_id, "alice")
        status = _wait_for(ctx, run_id, {"completed", "failed"})
        assert status == "completed", service.get_run(ctx, run_id).get("status_reason")
    finally:
        ctx.executor.stop()


def test_explorer_degraded_planner_pauses_with_deterministic_profile_and_nn13_label(tmp_path):
    ctx = _build_ctx(tmp_path, model_sonnet=None)
    ctx.executor.start()
    try:
        ctx.model_client = FakeModelClient(responses={})
        run_id = service.start_explorer_run(
            ctx, objective="Assess high value claims", sources=SOURCES,
            audit_period=AUDIT_PERIOD, run_owner="alice",
        )
        status = _wait_for(ctx, run_id, {"awaiting_confirmation", "failed"})
        assert status == "awaiting_confirmation", service.get_run(ctx, run_id).get("status_reason")

        state = ctx.persistence.load_state(run_id)
        assert state.plan["status"] == "llm_unavailable"
        assert state.plan["proposal"] is None
        # profile_result (the deterministic profile) is still there -- a
        # degraded planner pauses WITH what discover/profile already did,
        # never with nothing at all.
        assert state.profile_result is not None

        # NN13's exact label string (CLAUDE.md §3 non-negotiable 13 /
        # docs/specs/P6_narration_design.md §6.4's own labels table) --
        # see this file's module docstring and the test report for the gap
        # this closes in orchestrator/nodes/fieldwork.py.
        assert state.plan["label"] == "LLM unavailable — deterministic output only"

        trace = service.list_trace_events(ctx, run_id)
        assert any("LLM unavailable" in e["message"] for e in trace)

        with pytest.raises(ExplorerPlanNotConfirmable):
            service.confirm_plan(ctx, run_id, "alice")

        # superseding the stuck run moves it to `failed` with a reason --
        # a fresh context on the SAME local DB, now with a working Sonnet
        # endpoint (Settings is frozen; a retry with a configured
        # endpoint is a new AppContext, exactly what a redeployed/
        # reconfigured App would hand the next start_explorer_run call).
        ctx_retry = _build_ctx(tmp_path, worker_id="worker-retry")
        ctx_retry.executor.start()
        try:
            ctx_retry.model_client = FakeModelClient(responses={
                SONNET_ENDPOINT: _model_response(_wire_proposal(), served_model_version="sonnet-v1"),
            })
            new_run_id = service.start_explorer_run(
                ctx_retry, objective="Assess high value claims (retry)", sources=SOURCES,
                audit_period=AUDIT_PERIOD, run_owner="alice", supersedes_run_id=run_id,
            )
            assert new_run_id != run_id
            superseded = service.get_run(ctx_retry, run_id)
            assert superseded["status"] == "failed"
            assert "Superseded" in (superseded.get("status_reason") or "")
        finally:
            ctx_retry.executor.stop()
    finally:
        ctx.executor.stop()


def test_explorer_call_budget_never_exceeds_planner_plus_one_repair(tmp_path):
    """A repair round that ALSO fails validation still stops at 2 calls --
    the call-budget half of §8 item 12, driven through the real
    service/executor rather than _plan_explorer directly."""
    ctx = _build_ctx(tmp_path)
    ctx.executor.start()
    try:
        client = FakeModelClient(responses={
            SONNET_ENDPOINT: _model_response(_wire_proposal(t1_column="Amountx"), served_model_version="sonnet-v1"),
            GPT_OSS_ENDPOINT: _model_response(_wire_proposal(t1_column="StillWrong"), served_model_version="gpt-oss-v1"),
        })
        ctx.model_client = client
        run_id = service.start_explorer_run(
            ctx, objective="Assess high value claims", sources=SOURCES,
            audit_period=AUDIT_PERIOD, run_owner="alice",
        )
        status = _wait_for(ctx, run_id, {"awaiting_confirmation", "failed"})
        assert status == "awaiting_confirmation", service.get_run(ctx, run_id).get("status_reason")
        state = ctx.persistence.load_state(run_id)
        assert state.plan["status"] == "no_valid_tests"
        assert len(client.calls) == 2
        calls = ctx.persistence.list_llm_calls(run_id)
        assert len(calls) == 2
    finally:
        ctx.executor.stop()


def test_explorer_replay_mode_reproduces_an_identical_proposal_with_zero_live_calls(tmp_path):
    ctx1 = _build_ctx(tmp_path)
    ctx1.executor.start()
    try:
        client = FakeModelClient(responses={
            SONNET_ENDPOINT: _model_response(_wire_proposal(), served_model_version="sonnet-v1"),
        })
        ctx1.model_client = client
        run_id_1 = service.start_explorer_run(
            ctx1, objective="Assess high value claims", sources=SOURCES,
            audit_period=AUDIT_PERIOD, run_owner="alice",
        )
        status = _wait_for(ctx1, run_id_1, {"awaiting_confirmation", "failed"})
        assert status == "awaiting_confirmation", service.get_run(ctx1, run_id_1).get("status_reason")
        proposal_1 = ctx1.persistence.load_state(run_id_1).plan["proposal"]
        assert proposal_1
    finally:
        ctx1.executor.stop()

    ctx2 = service.build_app_context({
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(tmp_path / "data"),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports2"),
        "ORCH_WORKER_ID": "worker-b",
        "SKILLS_DIR": str(MINI_SKILLS_DIR),
        "CODE_REVISION": "test-fixed-revision",
        "AUDIT_TIMEZONE": "Australia/Sydney",
        "MODEL_SONNET": SONNET_ENDPOINT,
        "MODEL_GPT_OSS": GPT_OSS_ENDPOINT,
        "LLM_CACHE_MODE": "replay",
    })
    ctx2.executor.start()
    try:
        ctx2.model_client = RaisingModelClient(
            ModelUnavailable("should-not-be-called", "replay mode never calls live", permanent=True)
        )
        run_id_2 = service.start_explorer_run(
            ctx2, objective="Assess high value claims", sources=SOURCES,
            audit_period=AUDIT_PERIOD, run_owner="bob",
        )
        status = _wait_for(ctx2, run_id_2, {"awaiting_confirmation", "failed"})
        assert status == "awaiting_confirmation", service.get_run(ctx2, run_id_2).get("status_reason")
        proposal_2 = ctx2.persistence.load_state(run_id_2).plan["proposal"]
        assert proposal_2 == proposal_1

        calls_2 = ctx2.persistence.list_llm_calls(run_id_2)
        assert calls_2
        assert all(c["source"] == "cache" for c in calls_2), [c["source"] for c in calls_2]
    finally:
        ctx2.executor.stop()
