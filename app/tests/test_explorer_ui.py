"""Explorer Mode UI wiring (docs/specs/P6_P8_explorer_llm_design.md D2-D5):
the landing page's Explorer flow through the REAL Dash callbacks registered
by src/run_setup.py, against the REAL orchestrator.service + LocalPersistence
(overriding this directory's autouse fake_backend fixture -- same pattern as
test_app_startup.py) and a FakeModelClient (no network, CLAUDE.md §11).

Callbacks are invoked through app.callback_map, found by their declared
Input ids/properties (never a hardcoded output key -- Dash mangles a
duplicate-output callback's own map key with a content hash, e.g.
"d.children@<hash>", so matching by Input is the only stable lookup) and
called via `.__wrapped__` (the raw function `@app.callback` wraps, so no
Dash request/response plumbing is needed to call it directly)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd
import pytest
import yaml
from dash.exceptions import PreventUpdate

from orchestrator.adapters.model_fake import FakeModelClient
from src.platform import adapters

AUDIT_PERIOD = ("2026-01-01", "2026-02-28")
SONNET_ENDPOINT = "fake-sonnet-endpoint"
GPT_OSS_ENDPOINT = "fake-gptoss-endpoint"


def _write_skill(skills_dir: Path) -> None:
    d = skills_dir / "mini_explorer_source"
    d.mkdir(parents=True)
    contract = {
        "timezone": "Australia/Sydney",
        "sources": {
            "expense_report": {
                "format": "csv", "file": "expense_report.csv", "header_trim": True,
                "columns": {
                    "Employee ID": {"type": "integer", "nullable": False},
                    "Transaction Date": {"type": "date", "nullable": False},
                    "Amount": {"type": "number", "nullable": False},
                    "Category": {"type": "string", "nullable": False},
                    "Currency": {"type": "string", "nullable": False},
                },
            },
        },
    }
    (d / "contract.yaml").write_text(yaml.safe_dump(contract, sort_keys=False))


def _write_expense_data(data_root: Path) -> None:
    df = pd.DataFrame([
        {"Employee ID": 1, "Transaction Date": "2026-01-05", "Amount": 100, "Category": "Travel", "Currency": "AUD"},
        {"Employee ID": 2, "Transaction Date": "2026-01-10", "Amount": 700, "Category": "Travel", "Currency": "AUD"},
        {"Employee ID": 3, "Transaction Date": "2026-01-15", "Amount": 50, "Category": "Meals", "Currency": "AUD"},
        {"Employee ID": 4, "Transaction Date": "2026-02-01", "Amount": 900, "Category": "Travel", "Currency": "AUD"},
        {"Employee ID": 5, "Transaction Date": "2026-02-10", "Amount": 300, "Category": "Meals", "Currency": "AUD"},
    ])
    df.to_csv(data_root / "expense_report.csv", index=False)


def _wire_proposal(*, include_t2: bool = False) -> dict:
    """A single valid threshold_exceedance test/finding over "expense_report"
    ("t1"/"f1"), plus an optional second, independently-valid test/finding
    ("t2"/"f2") so excluding t2 in the UI still leaves t1 included (the
    "exclude one test" scenario, mirroring tests/test_explorer_service.py's
    own _wire_proposal shape -- self-contained here rather than imported,
    since tests/ has no __init__.py to import across as a package)."""
    metrics_t1 = [
        {"name": "hv_count", "kind": "count", "column": None, "key": None, "unit": "count", "where": None},
        {"name": "hv_amount", "kind": "sum", "column": "Amount", "key": None, "unit": "currency", "where": None},
    ]
    tests = [{
        "key": "t1", "name": "High value claims", "primitive": "threshold_exceedance",
        "params": {
            "kind": "threshold_exceedance", "population": "p1", "column": "Amount",
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
            "key": "t2", "name": "Meals claims", "primitive": "threshold_exceedance",
            "params": {
                "kind": "threshold_exceedance", "population": "p1", "column": "Amount",
                "limit": {"threshold": "meal_limit"}, "direction": "above",
                "group_by": None, "aggregate": None, "exclude": None,
                "metrics": [
                    {"name": "meal_count", "kind": "count", "column": None, "key": None,
                     "unit": "count", "where": None},
                ],
            },
            "control_key": "c1", "risk_key": "r1", "assertion": "operating",
            "control_objective": "Ensure claims stay within policy limits.",
            "risk_hypothesis": "Claims could exceed the approved limit without review.",
            "rationale": "A second, independent test so excluding it leaves the other test included.",
        })
        findings.append({
            "key": "f2", "test_key": "t2", "title": "Meals claims", "trigger": "meal_count > 0",
            "severity": [{"when": None, "then": "Low"}],
            "metrics_cited": ["meal_count"], "thresholds_cited": [], "monetary_basis": "none",
            "observation": "{meal_count} exceptions.",
            "recommendation": "No action required.",
            "management_questions": [],
        })
    thresholds = [{"id": "hv", "value": 500, "unit": "currency", "description": "High value limit"}]
    if include_t2:
        thresholds.append({"id": "meal_limit", "value": 20, "unit": "currency", "description": "Meal limit"})
    return {
        "schema_version": "explorer-plan/1",
        "skill_name": "Explorer High Value Test", "domain": "Travel and Expense",
        "summary": "Flags claims over a high value threshold.",
        "sources": [{"source": "expense_report", "amount_column": "Amount", "date_column": None,
                     "entry_key": ["Employee ID"]}],
        "populations": [{"key": "p1", "source": "expense_report", "description": "All claims", "filters": []}],
        "risks": [{"key": "r1", "title": "Overspend risk", "description": "Claims may exceed policy limits."}],
        "controls": [{"key": "c1", "risk_key": "r1", "title": "Threshold review",
                      "description": "Claims are reviewed against a limit.", "type": "detective"}],
        "thresholds": thresholds, "tests": tests, "findings": findings,
        "data_gaps": [], "assumptions": [],
    }


def _model_response(payload: dict, *, served_model_version: str = "v1"):
    from orchestrator.adapters.protocols import ModelResponse

    return ModelResponse(
        text=json.dumps(payload), served_model_version=served_model_version, finish_reason="stop",
        prompt_tokens=10, completion_tokens=10, total_tokens=20, request_id="req-1",
        reasoning_parts_stripped=0, latency_ms=5,
    )


def _build_real_service_env(tmp_path: Path, *, model_sonnet: str | None = SONNET_ENDPOINT) -> dict:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_expense_data(data_dir)

    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(data_dir),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
        "ORCH_WORKER_ID": "explorer-ui-test",
        "SKILLS_DIR": str(skills_dir),
        "CODE_REVISION": "test-fixed-revision",
        "AUDIT_TIMEZONE": "Australia/Sydney",
    }
    if model_sonnet:
        env["MODEL_SONNET"] = model_sonnet
        env["MODEL_GPT_OSS"] = GPT_OSS_ENDPOINT
    return env


def _real_ctx_for(monkeypatch, env: dict):
    """Points app/'s adapters at the REAL orchestrator.service +
    LocalPersistence (overriding this directory's autouse fake_backend
    fixture -- same pattern as test_app_startup.py)."""
    from orchestrator import service as real_service

    monkeypatch.setattr(adapters, "service", real_service)
    adapters._ctx = None
    original_build = real_service.build_app_context
    monkeypatch.setattr(real_service, "build_app_context", lambda *a, **k: original_build(env))
    return adapters.get_context()


@pytest.fixture
def real_ctx(monkeypatch, tmp_path):
    ctx = _real_ctx_for(monkeypatch, _build_real_service_env(tmp_path))
    ctx.executor.start()
    yield ctx
    ctx.executor.stop()
    adapters._ctx = None


def _wait_for(ctx, run_id, statuses, timeout=15):
    from orchestrator import service as real_service

    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = real_service.get_run(ctx, run_id)["status"]
        if last in statuses:
            return last
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach {statuses} in time (last={last})")


def _find_callback(app, *, inputs: list[tuple[str, str]]):
    want = [{"id": i, "property": p} for i, p in inputs]
    for entry in app.callback_map.values():
        if entry.get("inputs") == want:
            return entry["callback"].__wrapped__
    available = [e.get("inputs") for e in app.callback_map.values()]
    raise AssertionError(f"no callback registered for inputs {want}; available: {available}")


def _walk_ids(node, out=None):
    if out is None:
        out = {}
    if node is None or isinstance(node, (str, int, float)):
        return out
    if isinstance(node, (list, tuple)):
        for n in node:
            _walk_ids(n, out)
        return out
    _id = getattr(node, "id", None)
    if isinstance(_id, str):
        out[_id] = node
    children = getattr(node, "children", None)
    if children is not None:
        _walk_ids(children, out)
    return out


def _make_app(real_ctx):
    """A real Dash app with run_setup's callbacks registered -- the app.py
    module itself (loaded via conftest's load_app_entry) also registers
    run_status/workspace_tne, but this file never touches those two
    (another agent owns them), so a bare app + register_callbacks is enough
    and avoids importing app.py's own executor-start side effects twice."""
    import dash

    from src import run_setup

    app = dash.Dash(__name__)
    app.layout = lambda: run_setup.home_layout()
    run_setup.register_callbacks(app)
    return app


def _fake_request(monkeypatch, owner="explorer-auditor@example.com"):
    monkeypatch.setattr(adapters, "is_local_backend", lambda: True)
    import flask
    ctx = flask.Flask(__name__).test_request_context("/", headers={"X-Forwarded-Email": owner})
    ctx.push()
    return ctx


def test_full_explorer_flow_via_dash_callbacks(monkeypatch, real_ctx):
    """Start -> review rows (workflow_stage + include/exclude Checklist,
    D3/D4) -> exclude one test -> confirm -> run page, entirely through the
    registered Dash callbacks, against the real service/LocalPersistence."""
    from src.run_setup import _explorer_source_options

    app = _make_app(real_ctx)
    flask_ctx = _fake_request(monkeypatch)
    try:
        real_ctx.model_client = FakeModelClient(
            responses={SONNET_ENDPOINT: _model_response(_wire_proposal(include_t2=True))}
        )

        # D5a: the auditor ticks the checklist rather than anything being
        # auto-picked -- this is the same option list
        # refresh_explorer_source_checklist would have offered.
        selected_sources = [opt["value"] for opt in _explorer_source_options("")]
        assert selected_sources, "expected at least one governed source option"

        start_new_objective = _find_callback(app, inputs=[("explorer-start-btn", "n_clicks")])
        store_data, summary = start_new_objective(
            1, "Assess high value claims", AUDIT_PERIOD[0], AUDIT_PERIOD[1], None, None, None,
            selected_sources,
        )
        assert store_data and store_data.get("run_id"), summary
        run_id = store_data["run_id"]

        status = _wait_for(real_ctx, run_id, {"awaiting_confirmation", "failed"})
        assert status == "awaiting_confirmation", adapters.get_run(run_id)

        render_workflow_preview = _find_callback(app, inputs=[
            ("audit-objective", "value"), ("selected-mode-store", "data"),
            ("explorer-run-store", "data"), ("explorer-poll-interval", "n_intervals"),
        ])
        children, interval_disabled = render_workflow_preview(None, "explorer", store_data, 0)
        assert interval_disabled is True  # plan phase finished -- no more idle polling (§11)

        rendered_ids = _walk_ids(children)
        checklist = rendered_ids.get("explorer-test-checklist")
        assert checklist is not None, "expected the D4 include/exclude Checklist to render"
        checklist_values = {opt["value"] for opt in checklist.options}
        assert checklist_values == {"t1", "t2"}
        assert set(checklist.value) == {"t1", "t2"}  # both proposed valid, both included by default

        edit_explorer_checklist = _find_callback(app, inputs=[("explorer-test-checklist", "value")])
        with pytest.raises(PreventUpdate):
            edit_explorer_checklist(["t1"], store_data)  # exclude t2, keep t1

        review = adapters.get_explorer_review(run_id)
        included = {t["key"] for t in review["tests"] if t["included"]}
        assert included == {"t1"}

        start_run = _find_callback(app, inputs=[("start-run-btn", "n_clicks")])
        pathname, summary2 = start_run(
            1, "Assess high value claims", AUDIT_PERIOD[0], AUDIT_PERIOD[1], None, None, [],
            None, store_data,
        )
        assert pathname == f"/run/{run_id}", summary2

        status = _wait_for(real_ctx, run_id, {"awaiting_signoff", "failed"})
        assert status == "awaiting_signoff", adapters.get_run(run_id)

        adapters.sign_off(run_id, "explorer-auditor@example.com")
        status = _wait_for(real_ctx, run_id, {"completed", "failed"})
        assert status == "completed", adapters.get_run(run_id)

        # /run/<id> renders for an Explorer run without crashing -- owned by
        # another agent's file (run_status.py), used here read-only.
        from src import run_status

        page = run_status.run_page(run_id)
        assert page is not None

        findings = real_ctx.persistence.list_findings(run_id)
        assert len(findings) == 1  # only t1's finding rule -- t2 was excluded before confirm
        assert findings[0]["rule_id"].startswith(f"EXPLORER-{run_id}.")

        # Save as draft Skill (D5.1's own final step) -- appears in /skills.
        save_explorer_draft = _find_callback(app, inputs=[("explorer-save-draft-btn", "n_clicks")])
        panel = save_explorer_draft(1, store_data)
        panel_text = str(panel)
        assert "Saved as draft Skill" in panel_text

        skills = adapters.list_skills()
        drafts = [s for s in skills if s["skill_id"].startswith("SKILL-X-")]
        assert len(drafts) == 1
        assert drafts[0]["status"] == "Draft"
        assert drafts[0]["has_workspace"] is False

        draft_detail = adapters.get_skill(drafts[0]["skill_id"])
        assert draft_detail is not None
        assert draft_detail["status"] == "Draft"
    finally:
        flask_ctx.pop()


def test_explorer_degrades_gracefully_when_planner_unavailable(monkeypatch, tmp_path):
    """MODEL_SONNET unset (model_sonnet=None): the gateway's own step 1
    ("endpoint not configured for role") returns `unavailable` WITHOUT ever
    calling the ModelClient (orchestrator/llm/gateway.py §3.6), so
    FakeModelClient(responses={}) never actually needs an entry here --
    unlike a configured endpoint that the model genuinely fails to answer,
    which is a different (untested-here) failure shape."""
    from src.run_setup import _explorer_source_options

    real_ctx = _real_ctx_for(monkeypatch, _build_real_service_env(tmp_path, model_sonnet=None))
    real_ctx.executor.start()
    app = _make_app(real_ctx)
    flask_ctx = _fake_request(monkeypatch)
    try:
        real_ctx.model_client = FakeModelClient(responses={})

        selected_sources = [opt["value"] for opt in _explorer_source_options("")]
        assert selected_sources, "expected at least one governed source option"

        start_new_objective = _find_callback(app, inputs=[("explorer-start-btn", "n_clicks")])
        store_data, summary = start_new_objective(
            1, "Assess high value claims", AUDIT_PERIOD[0], AUDIT_PERIOD[1], None, None, None,
            selected_sources,
        )
        assert store_data and store_data.get("run_id"), summary
        run_id = store_data["run_id"]

        status = _wait_for(real_ctx, run_id, {"awaiting_confirmation", "failed"})
        assert status == "awaiting_confirmation", adapters.get_run(run_id)

        review = adapters.get_explorer_review(run_id)
        assert review["llm_unavailable"] is True
        # orchestrator.nodes.fieldwork._plan_explorer sets plan.label=None
        # for an unset-endpoint "unavailable" (the label string only ever
        # fills in from a live call's own r1.error) -- the UI's own fallback
        # (src/run_setup._explorer_workflow_children) is what actually
        # guarantees the CLAUDE.md §6 NN13 label text reaches the screen.

        render_workflow_preview = _find_callback(app, inputs=[
            ("audit-objective", "value"), ("selected-mode-store", "data"),
            ("explorer-run-store", "data"), ("explorer-poll-interval", "n_intervals"),
        ])
        children, interval_disabled = render_workflow_preview(None, "explorer", store_data, 0)
        assert interval_disabled is True
        assert "LLM unavailable — deterministic output only" in str(children)

        from orchestrator.errors import ExplorerPlanNotConfirmable

        with pytest.raises(ExplorerPlanNotConfirmable):
            adapters.confirm_plan(run_id, "explorer-auditor@example.com")
    finally:
        flask_ctx.pop()
        real_ctx.executor.stop()
        adapters._ctx = None


def test_no_idle_polling_before_any_explorer_run_starts(real_ctx):
    """CLAUDE.md §11 cost incident: the Interval must stay disabled with no
    active Explorer run -- both on initial page load (default store) and
    while browsing in Playbook mode."""
    app = _make_app(real_ctx)
    render_workflow_preview = _find_callback(app, inputs=[
        ("audit-objective", "value"), ("selected-mode-store", "data"),
        ("explorer-run-store", "data"), ("explorer-poll-interval", "n_intervals"),
    ])

    _children, disabled = render_workflow_preview(None, "playbook", None, None)
    assert disabled is True

    _children, disabled = render_workflow_preview(None, "explorer", None, None)
    assert disabled is True  # mode selected, but no run started yet


def test_polling_enabled_only_while_the_explorer_run_is_actively_working(monkeypatch, real_ctx):
    """CLAUDE.md §11 cost incident: `explorer-poll-interval.disabled` must
    track the run's REAL status, in both directions -- enabled while the
    plan node is still queued/running, disabled the moment it reaches a
    terminal plan-phase status. get_explorer_review's own run_status is
    stubbed here (rather than racing the real background executor) so both
    directions are asserted deterministically."""
    app = _make_app(real_ctx)
    render_workflow_preview = _find_callback(app, inputs=[
        ("audit-objective", "value"), ("selected-mode-store", "data"),
        ("explorer-run-store", "data"), ("explorer-poll-interval", "n_intervals"),
    ])
    store_data = {"run_id": "RUN-EXPLORER-1"}

    monkeypatch.setattr(adapters, "get_explorer_review", lambda run_id: {
        "run_status": "running", "plan_status": None, "llm_unavailable": False, "label": None,
        "tests": [], "n_valid": 0, "n_total": 0, "proposal_errors": [],
    })
    _children, disabled = render_workflow_preview(None, "explorer", store_data, 0)
    assert disabled is False

    monkeypatch.setattr(adapters, "get_explorer_review", lambda run_id: {
        "run_status": "awaiting_confirmation", "plan_status": "proposed", "llm_unavailable": False,
        "label": None, "tests": [], "n_valid": 0, "n_total": 0, "proposal_errors": [],
    })
    _children, disabled = render_workflow_preview(None, "explorer", store_data, 1)
    assert disabled is True


def test_explorer_runs_are_listed_on_runs_trace_and_actions(monkeypatch, real_ctx):
    """D5: Explorer results appear on /runs, /trace and /actions like
    Playbook runs -- and /workspace/tne is unaffected (SKILL-001 only)."""
    from orchestrator.adapters.model_fake import FakeModelClient
    from orchestrator import service as real_service

    real_ctx.model_client = FakeModelClient(
        responses={SONNET_ENDPOINT: _model_response(_wire_proposal())}
    )
    run_id = real_service.start_explorer_run(
        real_ctx, objective="Assess high value claims",
        sources=[{"kind": "local_file", "ref": "expense_report.csv"}],
        audit_period=AUDIT_PERIOD, run_owner="explorer-auditor@example.com",
    )
    _wait_for(real_ctx, run_id, {"awaiting_confirmation", "failed"})
    real_service.confirm_plan(real_ctx, run_id, "explorer-auditor@example.com")
    _wait_for(real_ctx, run_id, {"awaiting_signoff", "failed"})
    real_service.sign_off(real_ctx, run_id, "explorer-auditor@example.com")
    _wait_for(real_ctx, run_id, {"completed", "failed"})

    runs = adapters.list_audit_runs()
    matching = [r for r in runs if r["run_id"] == run_id]
    assert len(matching) == 1
    assert matching[0]["skill_name"].startswith("Explorer:")

    trace = adapters.list_trace_events(run_id)
    assert any(e["event_type"] == "plan_confirmed" for e in trace)

    actions = adapters.list_management_actions(filters={"run_id": run_id})
    assert isinstance(actions, list)  # never crashes for a skill_id-less-in-repo Explorer run


def test_explorer_source_options_reuses_governed_data_search(monkeypatch, real_ctx):
    """(D5a) _explorer_source_options is the checklist's real options
    builder -- it reuses adapters.search_governed_data the same way the old
    auto-pick (_explorer_source_candidates) did, but returns every eligible
    option for the auditor to tick rather than silently choosing for them."""
    import json

    from src.run_setup import _explorer_source_options

    flask_ctx = _fake_request(monkeypatch)
    try:
        options = _explorer_source_options("")
    finally:
        flask_ctx.pop()
    decoded = [json.loads(opt["value"]) for opt in options]
    assert {"kind": "local_file", "ref": "expense_report.csv"} in decoded
    matching = [opt for opt in options if opt["label"] == "expense_report.csv"]
    assert len(matching) == 1


def test_landing_page_default_render_unchanged_by_explorer_wiring():
    """Parity guard local to this file: home_layout() with no argument
    (every route except the D5.1 "?mode=explorer" one) must still render
    the playbook-selected state -- app/tests/test_layout_parity.py is the
    authoritative zero-diff check; this only guards the default_mode
    plumbing added here specifically."""
    from src.run_setup import home_layout

    layout = home_layout()
    ids = _walk_ids(layout)
    assert "explorer-section" in ids
    assert ids["explorer-section"].style == {"display": "none"}
    assert "playbook-skills-section" in ids
    assert ids["playbook-skills-section"].style == {}
