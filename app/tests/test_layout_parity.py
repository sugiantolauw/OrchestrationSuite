"""Layout parity against the prototype (reference_app/src/platform/pages.py
+ components.py): for each page, the set of element ids and the ordered
tree of (tag, id, className) must match the prototype's rendering of the
SAME data, exactly. No allow-list: every page covered here must diverge in
zero elements. /run/<id> is not covered at all (the prototype has no
equivalent page).

This is a regression lock: future changes to app/'s ported pages that add,
remove, relabel, disable or restyle an element will fail this test.

/workspace/tne and /skills/<id> use a separate, looser harness further down
(landmarks.py) instead of the zero-diff comparison above: both are driven by
data whose row COUNT legitimately differs from the prototype's own fixed/
random demo data (CLAUDE.md item E), so a zero-diff tree comparison would
fail on volume alone. That harness checks structure that must still match --
section headings, tab ids, KPI labels, chart ids, table headers -- against a
real completed local-backend run over synthetic_data/, and is slow (~3 min,
one xlsx-heavy pipeline run) and skipped when synthetic_data/ is absent.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_APP_DIR = os.path.dirname(_TESTS_DIR)
_REPO_ROOT = os.path.dirname(_APP_DIR)

sys.path.insert(0, _TESTS_DIR)
from tree import shape, render  # noqa: E402


def _reference_tree(page: str) -> str:
    result = subprocess.run(
        [sys.executable, os.path.join(_TESTS_DIR, "reference_tree.py"), _REPO_ROOT, page],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.rstrip("\n")


@pytest.fixture(scope="module")
def reference_fixtures():
    result = subprocess.run(
        [sys.executable, os.path.join(_TESTS_DIR, "dump_reference_fixtures.py"), _REPO_ROOT],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def _app_tree(component) -> str:
    return render(shape(component))


def test_landing_page_matches_prototype(monkeypatch, reference_fixtures):
    from src.platform import adapters
    from src.run_setup import home_layout

    assets = reference_fixtures["DEMO_DATA_ASSETS"]
    monkeypatch.setattr(adapters, "list_skills", lambda filters=None: reference_fixtures["DEMO_SKILLS"])
    monkeypatch.setattr(
        adapters, "search_governed_data",
        lambda q, limit=None: (
            assets if not q else [a for a in assets if q.lower() in a["name"].lower()]
        )[:limit] if limit is not None else (
            assets if not q else [a for a in assets if q.lower() in a["name"].lower()]
        ),
    )
    monkeypatch.setattr(adapters, "is_demo_mode", lambda: True)
    # Any string works here: the tree comparison is structural (tag/id/
    # className) only, never text content, so this need not (and must not,
    # tests/test_portability.py NN16) be the prototype's own hardcoded path.
    monkeypatch.setattr(adapters, "get_upload_base_path", lambda: "/Volumes/placeholder/uploads")

    assert _app_tree(home_layout()) == _reference_tree("landing")


def test_skill_library_page_matches_prototype(monkeypatch, reference_fixtures):
    from src.platform import adapters
    from src.platform.pages import skill_library_page

    monkeypatch.setattr(adapters, "list_skills", lambda filters=None: reference_fixtures["DEMO_SKILLS"])
    monkeypatch.setattr(adapters, "is_demo_mode", lambda: True)

    assert _app_tree(skill_library_page()) == _reference_tree("skills")


def test_audit_runs_page_matches_prototype(monkeypatch, reference_fixtures):
    from src.platform import adapters
    from src.platform.pages import audit_runs_page

    monkeypatch.setattr(adapters, "list_audit_runs", lambda filters=None: reference_fixtures["DEMO_AUDIT_RUNS"])
    monkeypatch.setattr(adapters, "is_demo_mode", lambda: True)

    assert _app_tree(audit_runs_page()) == _reference_tree("runs")


def test_platform_trace_page_matches_prototype(monkeypatch, reference_fixtures):
    from src.platform import adapters
    from src.platform.pages import platform_trace_page

    monkeypatch.setattr(adapters, "list_audit_runs", lambda filters=None: reference_fixtures["DEMO_AUDIT_RUNS"])
    monkeypatch.setattr(adapters, "list_trace_events", lambda run_id=None: reference_fixtures["DEMO_TRACE_EVENTS"])
    monkeypatch.setattr(adapters, "is_demo_mode", lambda: True)

    assert _app_tree(platform_trace_page()) == _reference_tree("trace")


# Allow-list: exactly one deviation on /actions, both named here and in
# app/src/platform/pages.py at the removed call site. User decision
# 2026-09-24: the prototype's "Session-only persistence in demo mode --
# actions reset when the app restarts" notice is REMOVED from app/'s
# management_actions_page -- it is false on the real (Delta) backend, where
# actions persist across restarts. Nothing else on the page changes. The
# reference tree still renders the prototype's own (untouched) version of
# this notice -- an anonymous `Div > Span, Span` block, id=None/class=None
# throughout, so it cannot be filtered by id or className -- as the 3rd
# top-level child (after page-header and the demo indicator, before the KPI
# row); stripped out here as an exact, position-anchored substring so this
# stays a zero-diff comparison in every other respect, and so a future
# reshuffle of the prototype's page that moves this substring elsewhere
# makes this assertion fail loudly rather than silently stop filtering.
_ACTIONS_REMOVED_NOTICE_BLOCK = (
    "\n  Div id=None class=None\n"
    "    Span id=None class=None\n"
    "    Span id=None class=None\n"
    "  Div id=None class='plat-kpi-row'"
)


def test_management_actions_page_matches_prototype(monkeypatch, reference_fixtures):
    from src.platform import adapters
    from src.platform.pages import management_actions_page

    monkeypatch.setattr(
        adapters, "list_management_actions", lambda filters=None: reference_fixtures["DEMO_MANAGEMENT_ACTIONS"]
    )
    monkeypatch.setattr(adapters, "is_demo_mode", lambda: True)

    reference = _reference_tree("actions")
    assert _ACTIONS_REMOVED_NOTICE_BLOCK in reference, (
        "the prototype's session-only-persistence notice block moved or changed shape -- "
        "update _ACTIONS_REMOVED_NOTICE_BLOCK rather than silently losing this allow-list entry"
    )
    reference_without_removed_notice = reference.replace(
        _ACTIONS_REMOVED_NOTICE_BLOCK, "\n  Div id=None class='plat-kpi-row'"
    )
    assert _app_tree(management_actions_page()) == reference_without_removed_notice


# ── Matching-volume harness: /workspace/tne and /skills/<id> ────────────────
#
# Both pages are driven by data whose ROW COUNT legitimately differs from
# the prototype's own fixed/random demo data (the real SKILL-001 test
# catalogue vs. reference_app's hardcoded one; a real completed run's
# findings vs. its seeded-random RF_* flags) -- a zero-diff structural
# comparison like the pages above would fail on volume alone, not on any
# real divergence. landmarks.py instead extracts section headings, tab ids,
# KPI labels, chart ids and table headers (with any run-specific number
# normalised to "#") and never descends into a table body, so a difference
# here means an actual missing/renamed/restyled element, not a different
# finding count.

import time as _time  # noqa: E402

_SYNTHETIC_DATA_DIR = os.path.join(_REPO_ROOT, "synthetic_data")


def _reference_landmarks(page: str) -> dict:
    result = subprocess.run(
        [sys.executable, os.path.join(_TESTS_DIR, "reference_landmarks.py"), _REPO_ROOT, page],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def real_completed_tne_run(tmp_path_factory):
    """One completed local-backend run over the real synthetic_data/
    (CLAUDE.md item E: "produce a completed local-backend run"), built once
    for every test in this module that needs it -- genuinely slow (xlsx
    parsing, same cost tests/test_p3_synthetic_full_run.py's docstring
    describes), so it is not part of the fast app/tests suite by default."""
    if not os.path.isdir(_SYNTHETIC_DATA_DIR):
        pytest.skip("synthetic_data/ not present in this checkout")

    from orchestrator import service as real_service

    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path_factory.mktemp("orch_db") / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": _SYNTHETIC_DATA_DIR,
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path_factory.mktemp("exports")),
        "ORCH_WORKER_ID": "layout-parity-landmarks",
        "SKILLS_DIR": os.path.join(_REPO_ROOT, "skills"),
        # Pinned rather than derived from `git rev-parse HEAD` -- several
        # agents commit to this checkout concurrently, so HEAD can move
        # between this run's creation and the executor's later fingerprint
        # re-verification pass (CLAUDE.md §4.1 "verified at every executor
        # pass"), which is a real drift worth catching in production but not
        # one this ~3-minute-long fixture is exercising (tests/test_p3_
        # service.py's own _build_ctx does the same, for the same reason).
        "CODE_REVISION": "test-fixed-revision",
    }
    ctx = real_service.build_app_context(env)
    ctx.executor.start()
    try:
        bindings = real_service.suggest_bindings(ctx, "SKILL-001")
        run_id = real_service.start_audit_run(
            ctx, skill_id="SKILL-001", bindings=bindings,
            audit_period=("2025-01-01", "2026-04-30"),
            objective="Layout parity landmark run.", run_owner="layout-parity-test",
        )

        def _wait(statuses, timeout):
            deadline = _time.time() + timeout
            status = None
            while _time.time() < deadline:
                status = real_service.get_run(ctx, run_id)["status"]
                if status in statuses:
                    return status
                _time.sleep(1)
            return status

        status = _wait({"awaiting_signoff", "failed"}, timeout=600)
        assert status == "awaiting_signoff", real_service.get_run(ctx, run_id).get("status_reason")

        real_service.sign_off(ctx, run_id, "layout-parity-test")
        status = _wait({"completed", "failed"}, timeout=60)
        assert status == "completed", real_service.get_run(ctx, run_id).get("status_reason")
    finally:
        ctx.executor.stop()

    yield real_service, ctx, run_id


# landmarks() does not capture "sub" paragraph text (only headings, KPI
# titles and table headers -- see landmarks.py's own docstring), so neither
# of these two intentional wording changes trips any assertion below. Named
# here anyway so a reviewer who extends landmarks() to also capture "sub"
# text finds the reason on file rather than a fresh unexplained failure.
_ALLOWED_TEXT_DIVERGENCES = {
    # text change: user decision 2026-09-24 (prototype wording untrue)
    "Priority is determined from severity, recurrence and financial exposure.":
        "Priority is determined from severity and financial exposure.",
    # text change: user decision 2026-09-24 (prototype wording untrue)
    "Action ownership and responses are maintained in this session for the showcase.":
        "Action ownership and responses are saved with this run.",
}


_DYNAMIC_IN_PROTOTYPE_NOTE = (
    "reference_app's finding cards and Management Action Tracker table are populated by "
    "session-store Dash callbacks (render_action_summary, the filtered-findings-container "
    "callback) that this static-component-tree harness never runs -- and CLAUDE.md "
    "non-negotiable 2 forbids this app from doing the same (audit evidence is never "
    "computed in a callback), so it renders that content directly instead. Both show the "
    "same content in a live browser; only a static render of the prototype's own page "
    "cannot see it. Excluded below by name/position, not because this app added them."
)


def test_workspace_tne_matches_prototype_landmarks(monkeypatch, real_completed_tne_run):
    from src.platform import adapters
    from src.workspace_tne import tne_workspace_layout
    from landmarks import landmarks, _normalize

    real_service, ctx, run_id = real_completed_tne_run
    monkeypatch.setattr(adapters, "service", real_service)
    monkeypatch.setattr(adapters, "get_context", lambda: ctx)

    payload = real_service.get_run_payload(ctx, run_id)
    finding_headings = {_normalize(f.get("title", "")) for f in payload.get("findings", [])}

    app_lm = landmarks(tne_workspace_layout(run_id))
    ref_lm = _reference_landmarks("workspace_tne")

    # Section headings, tab ids and chart ids match the prototype exactly,
    # except this run's own finding titles (see _DYNAMIC_IN_PROTOTYPE_NOTE).
    # Independent review 2026-09-24 item 5: the "Run signoff" panel this
    # allow-list used to name was removed from /workspace/tne entirely (the
    # approved HITL sign-off gate's self-approval text belongs only where
    # the prototype shows who signed off -- /run/<id> and /runs, CLAUDE.md
    # §11) -- there is no longer a deviation to name here.
    app_headings = [h for h in app_lm["headings"] if h not in finding_headings]
    assert app_headings == ref_lm["headings"], _DYNAMIC_IN_PROTOTYPE_NOTE
    assert app_lm["tab_ids"] == ref_lm["tab_ids"]
    assert app_lm["chart_ids"] == ref_lm["chart_ids"]

    # KPI labels match exactly except "Open management actions", which the
    # prototype fills into its 4th executive-brief tile via the same
    # render_action_summary callback.
    app_kpis = list(app_lm["kpi_labels"])
    app_kpis.remove("Open management actions")
    assert app_kpis == ref_lm["kpi_labels"], _DYNAMIC_IN_PROTOTYPE_NOTE

    # Table headers: FULL header sets, no positional slicing (independent
    # review 2026-09-24 item 5) -- the one remaining gap from the
    # prototype's own table_headers is named explicitly below by VALUE (a
    # contiguous block, removed only if it matches exactly), never silently
    # dropped by a blind `[:n]`/`[-n:]` comparison that could hide an
    # unrelated divergence anywhere else in the list.
    app_headers = list(app_lm["table_headers"])
    ref_headers = list(ref_lm["table_headers"])
    # The Management Action Tracker table's own headers -- session-store
    # rendered in the prototype (_DYNAMIC_IN_PROTOTYPE_NOTE), so absent from
    # ref_headers entirely; this app renders that table's markup directly
    # instead (same content, different mechanism). Named explicitly, in the
    # exact order app/src/platform/components.py's action table declares
    # them, and only removed if found verbatim right after "Matters
    # requiring attention"'s own 4 headers -- everything else, including
    # the methodology table's Layer/What it proves, must match with zero
    # exclusion.
    _ACTION_TRACKER_HEADERS = ["Test", "Finding", "Risk", "Status", "Owner", "Target", "Exposure", ""]
    matters_header_count = len(["Risk", "Matter", "Exposure", "Exceptions"])
    found = app_headers[matters_header_count:matters_header_count + len(_ACTION_TRACKER_HEADERS)]
    assert found == _ACTION_TRACKER_HEADERS, (
        f"expected the Management Action Tracker's own header block right after Matters "
        f"requiring attention's headers, got {found} -- update this allow-list by value, "
        f"never re-introduce a positional slice"
    )
    del app_headers[matters_header_count:matters_header_count + len(_ACTION_TRACKER_HEADERS)]
    assert app_headers == ref_headers, _DYNAMIC_IN_PROTOTYPE_NOTE


def test_skill_methodology_page_matches_prototype_landmarks(monkeypatch, real_completed_tne_run):
    from src.platform import adapters
    from src.platform.pages import skill_methodology_page
    from landmarks import landmarks

    real_service, ctx, _run_id = real_completed_tne_run
    monkeypatch.setattr(adapters, "service", real_service)
    monkeypatch.setattr(adapters, "get_context", lambda: ctx)

    app_lm = landmarks(skill_methodology_page("SKILL-001"))
    ref_lm = _reference_landmarks("methodology")

    assert app_lm == ref_lm
