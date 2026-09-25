"""UI-1 to UI-4 (docs/specs/P6_narration_design.md §7): the /run/<id>
"Model-written text for review" panel, the "AI-proposed findings" panel,
sign-off blocked while a candidate is undecided, Regenerate, and the
stale-confirm restart control (CLAUDE.md §11 "Paused runs across a code
deploy"). Independent review 2026-09-24's own criticism of fake-only UI
tests applies here more than anywhere else in app/: these are the ONE
place a fake could hide a genuine mismatch between what
orchestrator.service actually raises/returns and what this page assumes,
so every behavioural test below runs against the REAL LocalPersistence
backend and the mini Skill fixture (tests/narration_test_support.py,
tests/n9_test_support.py), never app/tests/fake_service.py. Only pure
rendering of a hand-built dict (no backend call) uses fake data.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import flask
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
# app/tests/conftest.py puts the repo root on sys.path behind app/ (so
# `import src...` in app/ resolves first) -- but app/tests/ is ITSELF a
# directory literally named "tests", so the bare name `tests` is a
# namespace package straddling both app/tests/ and the repo root's own
# tests/ (no __init__.py in either). Left in conftest.py's order, `import
# tests.conftest` resolves to app/tests/conftest.py, which has no
# canonical_ts -- this reorders the repo root ahead of app/ so `tests.*`
# below resolves to the repo root's tests/ package (narration_test_support,
# n9_test_support, conftest's canonical_ts), the one this WP's own test
# harness needs (docs/specs/P6_narration_design.md §11 "driven against
# real LocalPersistence runs"). `app/`'s own path stays on sys.path (just
# behind the repo root now), so `import src...` elsewhere is unaffected.
_repo_root_str = str(_REPO_ROOT)
if _repo_root_str in sys.path:
    sys.path.remove(_repo_root_str)
sys.path.insert(0, _repo_root_str)

from dash._callback_context import context_value
from dash._utils import AttributeDict

from orchestrator import runs as runs_module
from orchestrator import service as real_service
from orchestrator.pipeline import run_phase
from tests.conftest import canonical_ts
from tests.n9_test_support import FIELDWORK_NODES_FOR, app_context_for
from tests.narration_test_support import (
    DispatchingModelClient,
    happy_responses,
    make_narration_harness,
)

from src import run_status
from src.platform import adapters


# ── Harness plumbing ─────────────────────────────────────────────────────


def _clock():
    state = {"n": 0}

    def _c() -> str:
        state["n"] += 1
        return canonical_ts(state["n"])

    return _c


def _candidate_row(run_id: str, *, candidate_id: str, rule_id: str, **overrides) -> dict:
    row = {
        "candidate_id": candidate_id, "engagement_id": "ENG-DEFAULT", "skill_id": "SKILL-MINI",
        "generation": 0, "rule_id": rule_id, "title": "An AI-proposed candidate finding",
        "metrics_cited": ["hv_count"], "producing_test_ids": ["T1"], "proposed_severity": "Medium",
        "severity_reason": "Model-proposed reason.", "rationale": "Model rationale.",
        "monetary_basis": "none", "monetary_basis_note": "no declared monetary basis",
        "exposure_amount": None, "headline_eligible": False,
        "headline_ineligible_reason": "monetary_basis is none", "candidate_status": "candidate",
        "call_id": f"CALL-{candidate_id}",
    }
    row.update(overrides)
    return row


def _candidate_observation_narrative(run_id: str, candidate_id: str, *, now: str) -> dict:
    return {
        "narrative_id": f"NAR-{candidate_id}-observation", "run_id": run_id, "engagement_id": "ENG-DEFAULT",
        "target_kind": "candidate", "target_id": candidate_id, "field": "observation",
        "version": 1, "generation": 0, "origin": "model",
        "template_text": "This AI-proposed finding notes an unusual pattern worth an auditor's review.",
        "sources": [{"placeholder": "{count:hv_count}", "source_field": "run_metrics.hv_count", "unit": "count"}],
        "call_ids": ["CALL-1"], "served_model_version": "test-model-v1", "violations": None,
        "updated_by": "narrate", "updated_at": now,
    }


@pytest.fixture()
def real_run(monkeypatch, tmp_path):
    """A real, awaiting_signoff mini-Skill run with real narration (the
    finding observations, theme, exec summary `happy_responses()` scripts)
    plus one hand-inserted, undecided AI-proposed candidate -- the same
    `write_candidates` shortcut tests/test_signoff_narration.py already
    uses to exercise the candidate lifecycle without a live model. Points
    `adapters.service`/`adapters.get_context` at this run's own real
    AppContext (executor=None -- this fixture drives phases explicitly via
    run_phase, never a background thread)."""
    clock = _clock()
    from orchestrator.adapters.persistence_local import LocalPersistence

    persistence = LocalPersistence(str(tmp_path / "ledger.db"))
    persistence.migrate()
    client = DispatchingModelClient(happy_responses())
    h = make_narration_harness(persistence, tmp_path, model_client=client)
    fingerprint = h.persistence.get_fingerprint(h.state.fingerprint_id)
    state = run_phase(
        h.persistence, h.state.run_id, nodes_for=FIELDWORK_NODES_FOR, skill=h.ctx, clock=clock,
        current_fingerprint=fingerprint,
    )
    assert state.status == "awaiting_signoff", state.status_reason
    run_id = state.run_id

    candidate_id = "C1"
    h.persistence.write_candidates(run_id, [_candidate_row(run_id, candidate_id=candidate_id, rule_id="SKILL-MINI.ai.aaa")], now=clock())
    h.persistence.upsert_narrative(_candidate_observation_narrative(run_id, candidate_id, now=clock()))

    ctx_app = app_context_for(h, clock=clock)
    monkeypatch.setattr(adapters, "service", real_service)
    monkeypatch.setattr(adapters, "get_context", lambda: ctx_app)

    return h, ctx_app, run_id, candidate_id


def _register():
    class _FakeApp:
        def __init__(self):
            self.callbacks: dict[str, callable] = {}

        def callback(self, *_args, **_kwargs):
            def decorator(fn):
                self.callbacks[fn.__name__] = fn
                return fn

            return decorator

    app = _FakeApp()
    run_status.register_callbacks(app)
    return app.callbacks


_probe_app = flask.Flask(__name__)


def _call(fn, *args, actor: str = "auditor@example.com"):
    """Every callback below eventually calls run_status._request_actor(),
    which reads the identity header off the active Flask request (CLAUDE.md
    §9A.1) -- there is no such request outside a real Dash dispatch, so
    tests need the same test_request_context wrapper
    test_run_status.py's own identity test already uses."""
    with _probe_app.test_request_context("/", headers={"X-Forwarded-Email": actor}):
        return fn(*args)


def _set_triggered(component_id: dict) -> None:
    """Dash's own `ctx.triggered_id` is a ContextVar populated by the real
    callback dispatcher -- this is the documented way to set it for a
    directly-invoked callback closure (the same `_FakeApp` pattern
    test_workspace_tne_callback_errors.py already uses for closures that do
    NOT read dash.ctx; the ones here do, so this fills in the one piece
    that pattern's own docstring says it does not cover)."""
    prop_id = json.dumps(component_id, sort_keys=True) + ".n_clicks"
    context_value.set(AttributeDict(
        triggered_inputs=[{"prop_id": prop_id, "value": 1}],
        inputs_list=[], states_list=[], inputs={}, states={}, outputs_list=[],
        response={"multi": True},
    ))


# ── UI-2: accept / reject validation ─────────────────────────────────────


def test_accept_without_severity_is_refused(real_run):
    h, ctx_app, run_id, candidate_id = real_run
    callbacks = _register()
    btn_id = {"type": "candidate-accept-btn", "index": candidate_id}
    _set_triggered(btn_id)

    out = _call(
        callbacks["_accept_candidate"],
        [1], [None], [{"type": "candidate-severity-dropdown", "index": candidate_id}], run_id,
    )
    assert "CandidateSeverityRequired" in str(out)
    candidates = {c["candidate_id"]: c for c in h.persistence.list_candidates(run_id)}
    assert candidates[candidate_id]["candidate_status"] == "candidate"


def test_reject_without_reason_is_refused(real_run):
    h, ctx_app, run_id, candidate_id = real_run
    callbacks = _register()
    btn_id = {"type": "candidate-reject-btn", "index": candidate_id}
    _set_triggered(btn_id)

    out = _call(
        callbacks["_reject_candidate"],
        [1], [None], [{"type": "candidate-reason-input", "index": candidate_id}], run_id,
    )
    assert "CandidateReasonRequired" in str(out)
    candidates = {c["candidate_id"]: c for c in h.persistence.list_candidates(run_id)}
    assert candidates[candidate_id]["candidate_status"] == "candidate"


def test_accept_with_severity_decides_the_candidate(real_run):
    h, ctx_app, run_id, candidate_id = real_run
    callbacks = _register()
    _set_triggered({"type": "candidate-accept-btn", "index": candidate_id})

    out = _call(
        callbacks["_accept_candidate"],
        [1], ["High"], [{"type": "candidate-severity-dropdown", "index": candidate_id}], run_id,
    )
    text = str(out)
    assert "Action blocked" not in text
    candidates = {c["candidate_id"]: c for c in h.persistence.list_candidates(run_id)}
    assert candidates[candidate_id]["candidate_status"] == "accepted"
    assert candidates[candidate_id]["decided_severity"] == "High"
    # UI-2: the model's own proposed severity/reason stay stored alongside
    # the auditor's decided one -- neither is overwritten by the other.
    assert candidates[candidate_id]["proposed_severity"] == "Medium"


# ── UI-3: sign-off blocked while undecided, then allowed ─────────────────


def test_signoff_blocked_then_allowed_once_the_candidate_is_decided(real_run):
    h, ctx_app, run_id, candidate_id = real_run
    callbacks = _register()

    blocked = _call(callbacks["_confirm_signoff"], 1, run_id)
    assert "decide every AI-proposed finding before sign-off" in str(blocked)
    state = h.persistence.load_state(run_id)
    assert state.status == "awaiting_signoff"

    h.persistence.decide_candidate_cas(
        candidate_id, decision="accepted", reason=None, decided_severity="High",
        actor="alice", now=_clock()(),
    )
    allowed = _call(callbacks["_confirm_signoff"], 1, run_id)
    assert "decide every AI-proposed finding" not in str(allowed)
    state = h.persistence.load_state(run_id)
    assert state.status != "awaiting_signoff"


# ── UI-1: a refused edit shows the validator's message ───────────────────


def test_refused_narrative_edit_shows_the_mismatched_number(real_run):
    h, ctx_app, run_id, candidate_id = real_run
    narration = adapters.get_narration_review(run_id)
    finding_with_text = next(f for f in narration["findings"] if f.get("narrative_id"))
    narrative_id = finding_with_text["narrative_id"]

    callbacks = _register()
    _set_triggered({"type": "narrative-edit-save-btn", "index": narrative_id, "list": False})

    out = _call(
        callbacks["_save_narrative_edit"],
        [1], ["This edit invents the number 987654, which cites nothing real."],
        [{"type": "narrative-edit-textarea", "index": narrative_id}], run_id,
    )
    text = str(out)
    assert "987654" in text
    assert "does not match" in text


# ── UI-4: Regenerate keeps decisions ──────────────────────────────────────


def test_regenerate_keeps_candidate_decisions(real_run):
    h, ctx_app, run_id, candidate_id = real_run
    h.persistence.decide_candidate_cas(
        candidate_id, decision="rejected", reason="not applicable this run", decided_severity=None,
        actor="alice", now=_clock()(),
    )
    callbacks = _register()

    out = _call(callbacks["_confirm_regenerate"], 1, run_id)
    assert "Action blocked" not in str(out)

    state = h.persistence.load_state(run_id)
    assert state.status == "queued"
    clock = _clock()
    fingerprint = h.persistence.get_fingerprint(state.fingerprint_id)
    state = run_phase(
        h.persistence, run_id, nodes_for=FIELDWORK_NODES_FOR, skill=h.ctx, clock=clock,
        current_fingerprint=fingerprint,
    )
    assert state.status == "awaiting_signoff", state.status_reason

    candidates = {c["candidate_id"]: c for c in h.persistence.list_candidates(run_id)}
    assert candidates[candidate_id]["candidate_status"] == "rejected"
    assert candidates[candidate_id]["decision_reason"] == "not applicable this run"


# ── Stale-confirm restart (CLAUDE.md §11 "Paused runs across a code
# deploy") ─────────────────────────────────────────────────────────────


def _write_tiny_mini_data(root):
    import pandas as pd

    pd.DataFrame([
        {"Employee ID": 1, "Transaction Date": "2026-01-05", "Amount": 100, "Vendor": "VendorA"},
        {"Employee ID": 2, "Transaction Date": "2026-01-10", "Amount": 700, "Vendor": "VendorB"},
    ]).to_csv(root / "claims.csv", index=False)
    pd.DataFrame([
        {"Employee ID": 1, "Transaction Date": "2026-01-05", "Vendor": "VendorA"},
        {"Employee ID": 2, "Transaction Date": "2026-01-10", "Vendor": "VendorB"},
    ]).to_csv(root / "register.csv", index=False)


def test_stale_confirm_shows_restart_control_and_it_creates_a_new_run(monkeypatch, tmp_path):
    mini_skill_dir = _REPO_ROOT / "tests" / "fixtures" / "skills" / "mini"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_tiny_mini_data(data_dir)

    base_env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(data_dir),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
        "SKILLS_DIR": str(mini_skill_dir.parent),
    }
    ctx_old = real_service.build_app_context({**base_env, "CODE_REVISION": "rev-old"})
    ctx_old.executor.start()
    try:
        bindings = real_service.suggest_bindings(ctx_old, "SKILL-MINI")
        run_id = real_service.start_audit_run(
            ctx_old, skill_id="SKILL-MINI", bindings=bindings, audit_period=("2026-01-01", "2026-02-28"),
            objective="stale-confirm test", run_owner="tester@example.com", review_plan_first=True,
        )

        import time

        deadline = time.time() + 15
        status = None
        while time.time() < deadline:
            status = real_service.get_run(ctx_old, run_id)["status"]
            if status == "awaiting_confirmation":
                break
            time.sleep(0.05)
        assert status == "awaiting_confirmation", real_service.get_run(ctx_old, run_id).get("status_reason")
    finally:
        ctx_old.executor.stop()

    # A redeploy: a SECOND context, same persistence/db, a DIFFERENT code
    # revision -- exactly what `scripts/deploy_app.py` changes underneath an
    # already-paused run.
    ctx_new = real_service.build_app_context({**base_env, "CODE_REVISION": "rev-new"})
    monkeypatch.setattr(adapters, "service", real_service)
    monkeypatch.setattr(adapters, "get_context", lambda: ctx_new)

    callbacks = _register()
    out = _call(callbacks["_confirm_plan"], 1, run_id)
    text = str(out)
    assert "run-restart-stale-btn" in text
    assert "RunCodeRevisionStale" in text

    pathname, body = _call(callbacks["_restart_stale"], 1, run_id)
    assert pathname is not None and pathname.startswith("/run/")
    new_run_id = pathname.rsplit("/", 1)[-1]
    assert new_run_id != run_id
    new_run = real_service.get_run(ctx_new, new_run_id)
    assert new_run["run_id"] == new_run_id
    old_run_row = ctx_new.persistence.get_run_row(run_id)
    assert old_run_row.get("superseded_by") == new_run_id


# ── Chip and tooltip render only for accepted AI findings, on /workspace/
# tne, once the run has actually completed (finalise has run) ────────────


def test_workspace_tne_chip_and_tooltip_render_only_for_accepted_ai_findings(monkeypatch, real_run):
    from src import workspace_tne

    h, ctx_app, run_id, candidate_id = real_run
    clock = _clock()
    h.persistence.decide_candidate_cas(
        candidate_id, decision="accepted", reason=None, decided_severity="High",
        actor="alice", now=clock(),
    )

    state = runs_module.sign_off(h.persistence, run_id, actor="alice", now=clock())
    assert state.status not in ("awaiting_signoff",)
    fingerprint = h.persistence.get_fingerprint(state.fingerprint_id)
    state = run_phase(
        h.persistence, run_id, nodes_for=FIELDWORK_NODES_FOR, skill=h.ctx, clock=clock,
        current_fingerprint=fingerprint,
    )
    assert state.status == "completed", state.status_reason

    monkeypatch.setattr(adapters, "service", real_service)
    monkeypatch.setattr(adapters, "get_context", lambda: ctx_app)

    findings = ctx_app.persistence.list_findings(run_id)
    ai_findings = [f for f in findings if f.get("origin") == "ai_proposed"]
    rule_findings = [f for f in findings if f.get("origin") != "ai_proposed"]
    assert ai_findings, "the accepted candidate should have reached findings via finalise"
    assert rule_findings, "the mini fixture's own rule findings should still be present"

    ai_card = str(workspace_tne._finding_card(0, ai_findings[0]))
    assert "AI-proposed, accepted by alice" in ai_card

    rule_card = str(workspace_tne._finding_card(0, rule_findings[0]))
    assert "AI-proposed, accepted by" not in rule_card
