"""Automated control inventory (coordinator's independent-review commitment,
2026-09-25): every interactive control on every real page must be either
genuinely wired to a callback, a genuinely resolvable link, or explicitly
recorded as intentionally inert with a reason. This is a REGRESSION LOCK
against the exact class of bug BUG-TRACE-1 (src/trace_page.py) and
BUG-RUNS-1 (src/runs_page.py) were: a control the prototype renders (and
this build faithfully ports, CLAUDE.md §11 "UI is the prototype's,
exactly") that nothing ever wires a callback to, so clicking or typing into
it silently does nothing.

Method: build each page's REAL layout -- against real `orchestrator.service`
+ `LocalPersistence` runs, never `fake_service.py` (this module's fixtures
override this directory's autouse `fake_backend` fixture the same way
tests/test_runs_page.py and tests/test_workspace_tne.py's real-backend tests
do) -- walk the returned component tree, and for every control of a type
CLAUDE.md's task named (html.Button, dcc.Link/html.A with an `href`,
dcc.Dropdown, dcc.Checklist, dcc.Input, dcc.Textarea, dcc.DatePickerRange,
dcc.Upload) OR any element at all whose `id` is a pattern-matching dict
(src/platform/components.py's `skill-select-card`/`mode-select-card` cards
are plain html.Div, so only the "pattern-matching id" clause catches them),
assert it is either:

  (a) an Input or State of some callback registered on the REAL, fully
      wired `app` (app/app.py) -- checked against `app.callback_map`,
      matching a pattern-matching id (ALL/MATCH/ALLSMALLER) the way Dash
      itself does, not by string equality;
  (b) an `html.A`/`dcc.Link` whose `href` resolves -- to a route
      app.py's own `route_page` recognises, or (for "#anchor" links,
      src/platform/pages.py's own methodology-page table of contents) to
      an `html.A(id="anchor")` target actually present on the SAME page; or
  (c) named in `_INTENTIONALLY_INERT` below, with a reason.

Every one of (a)/(b)/(c) is checked mechanically -- nothing here is
asserted by construction the way a hand-picked list of ids would be, so a
future control that quietly loses its callback (or gains one that never
actually matches its id) fails this test the same way BUG-TRACE-1 and
BUG-RUNS-1 would have, months before an auditor tries the button.

`_INTENTIONALLY_INERT` is empty except for exactly the entries this test
itself found and could not explain any other way -- see each entry's own
comment. It is not a place to silence a real gap found later; a genuinely
new gap belongs in the phase report, the same way the three entries below
were, not a quiet fourth line here."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from dash.dependencies import _Wildcard

from conftest import load_app_entry
from orchestrator.state import RunState
from orchestrator.timeutil import utc_now
from src import run_status, run_setup, workspace_tne
from src.platform import adapters
from src.platform.pages import (
    audit_runs_page,
    management_actions_page,
    platform_nav,
    platform_trace_page,
    skill_library_page,
    skill_methodology_page,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ── Generic component-tree walker ────────────────────────────────────────

# Exactly the task's own list, plus html.A/dcc.Link WITH an href (a bare
# html.A(id="anchor") with no href is a fragment-link TARGET, not a
# control -- collected into `anchors` below instead, never into the
# inventory itself).
_NAMED_CONTROL_TAGS = {
    "Button", "Dropdown", "Checklist", "Input", "Textarea", "DatePickerRange", "Upload",
}
_LINK_TAGS = {"A", "Link"}


def _walk(node, controls: list[tuple[str, object, str | None]], anchors: set[str]) -> None:
    if node is None:
        return
    if isinstance(node, (list, tuple)):
        for child in node:
            _walk(child, controls, anchors)
        return
    if isinstance(node, (str, int, float)):
        return
    tag = type(node).__name__
    node_id = getattr(node, "id", None)
    href = getattr(node, "href", None)

    if tag == "A" and node_id is not None and not href:
        anchors.add(node_id)

    is_named_control = tag in _NAMED_CONTROL_TAGS or (tag in _LINK_TAGS and href)
    is_pattern_id = isinstance(node_id, dict)
    if node_id is not None and (is_named_control or is_pattern_id):
        controls.append((tag, node_id, href))

    children = getattr(node, "children", None)
    if children is not None:
        _walk(children, controls, anchors)


def _collect_controls(page) -> tuple[list[tuple[str, object, str | None]], set[str]]:
    controls: list[tuple[str, object, str | None]] = []
    anchors: set[str] = set()
    _walk(page, controls, anchors)
    return controls, anchors


# ── Callback-binding check, matching pattern ids the way Dash itself does ──

def _canonical_raw_id(component_id):
    """A raw_inputs/raw_state entry's `component_id` -- either a plain
    string, or a dict whose wildcard values are real `_Wildcard`
    (ALL/MATCH/ALLSMALLER) instances (dash/dependencies.py)."""
    if isinstance(component_id, str):
        return component_id
    return {k: ("__WILDCARD__" if isinstance(v, _Wildcard) else v) for k, v in component_id.items()}


def _canonical_state_id(id_str: str):
    """`callback_map[...]["state"]` entries only carry the SERIALISED
    id (`DashDependency.to_dict()` -- dash/dependencies.py has no
    `raw_state` the way it has `raw_inputs`), a JSON object with sorted
    keys where a wildcard value is encoded as a length-1 array
    (`_Wildcard.to_json()`) -- e.g. '{"index":["ALL"],"type":"run-view-btn"}'."""
    if not id_str.startswith("{"):
        return id_str
    parsed = json.loads(id_str)
    return {k: ("__WILDCARD__" if isinstance(v, list) else v) for k, v in parsed.items()}


def _id_matches(control_id, canonical_cb_id) -> bool:
    if isinstance(canonical_cb_id, str):
        return control_id == canonical_cb_id
    if not isinstance(control_id, dict):
        return False
    if set(control_id.keys()) != set(canonical_cb_id.keys()):
        return False
    for key, value in canonical_cb_id.items():
        if value == "__WILDCARD__":
            continue
        if control_id.get(key) != value:
            return False
    return True


def _has_bound_callback(app, control_id) -> bool:
    for entry in app.callback_map.values():
        for raw_input in entry.get("raw_inputs") or []:
            if _id_matches(control_id, _canonical_raw_id(raw_input.component_id)):
                return True
        for state_entry in entry.get("state") or []:
            if _id_matches(control_id, _canonical_state_id(state_entry["id"])):
                return True
    return False


# ── href resolution ──────────────────────────────────────────────────────

# Every path app.py's own route_page recognises (app/app.py) -- kept as a
# literal list here (not imported) so a route this test can't see added
# doesn't silently start passing hrefs it was never told about; a future
# route added to BOTH app.py and this list is the intended way to extend
# this check, mirroring how a real new page would be added.
_ROUTED_EXACT_PATHS = {"/", "/skills", "/runs", "/trace", "/actions", "/workspace/tne"}
_ROUTED_PATH_PREFIXES = ("/skills/", "/run/")


def _href_resolves(href: str | None, anchors: set[str]) -> bool:
    if not href:
        return False
    if href.startswith("#"):
        return href[1:] in anchors
    path = href.split("?", 1)[0]
    return path in _ROUTED_EXACT_PATHS or any(path.startswith(p) for p in _ROUTED_PATH_PREFIXES)


# ── (c) intentionally inert, with a reason ──────────────────────────────

# CLAUDE.md §11 "UI is the prototype's, exactly" means these three controls
# were ported faithfully from reference_app/src/platform/pages.py's own
# Skill Library search/filter row -- but unlike /trace's `trace-run-filter`
# (BUG-TRACE-1) and /runs' `runs-skill-filter`/`runs-status-filter`
# (BUG-RUNS-1), which this build's own src/trace_page.py and
# src/runs_page.py already wire, NOTHING in src/ or tests/ wires a callback
# to any of these three -- confirmed here, not assumed, the same way
# BUG-TRACE-1/BUG-RUNS-1 were confirmed before being fixed. This is a REAL,
# UNFIXED GAP of the identical shape, found by this test and reported in
# this phase's report rather than silently wired (out of this task's scope)
# or silently dropped (this allow-list entry is what keeps that visible
# instead of the test just going green with no record of it).
_INTENTIONALLY_INERT: set[tuple[str, str]] = {
    ("Input", "skill-search"),
    ("Dropdown", "skill-domain-filter"),
    ("Dropdown", "skill-status-filter"),
}


def _check_page(app, page_name: str, page) -> None:
    controls, anchors = _collect_controls(page)
    failures = []
    for tag, control_id, href in controls:
        if isinstance(control_id, str) and (tag, control_id) in _INTENTIONALLY_INERT:
            continue
        if _has_bound_callback(app, control_id):
            continue
        if _href_resolves(href, anchors):
            continue
        failures.append(f"{tag} id={control_id!r} href={href!r}")
    assert not failures, (
        f"{page_name}: control(s) with no bound callback, no resolvable href, and no "
        f"_INTENTIONALLY_INERT entry:\n  " + "\n  ".join(failures)
    )


# ── Real backend fixtures ────────────────────────────────────────────────

def _fingerprint(fp_id: str) -> dict:
    return dict(
        fingerprint_id=fp_id, source_table_versions="{}", uploaded_file_hashes="{}",
        reference_data_hashes="{}", skill_content_hash=None, code_revision="rev1",
        dependency_lock_hash="dep1", runtime_config_hash="rc1", endpoint_config="{}",
        prompt_template_version="none", created_at=utc_now(),
    )


def _make_bare_run(ctx, run_id: str, *, status: str, skill_id: str | None, mode: str) -> None:
    """A minimal, directly-persisted RunState (the same shape
    tests/test_runs_page.py's own `_make_run` uses) -- cheap enough to build
    one per status, against the REAL LocalPersistence backend, without
    running the actual multi-second pipeline seven times over."""
    now = utc_now()
    state = RunState(
        run_id=run_id, run_kind="fieldwork", engagement_id="ENG-DEFAULT",
        skill_id=skill_id, mode=mode, phase="plan",
        audit_period=("2026-01-01", "2026-01-31"), objective=f"control inventory {run_id}",
        run_owner="inventory-test", fingerprint_id=f"FP-{run_id}",
        created_at=now, last_state_change_at=now, status=status,
    )
    ctx.persistence.create_run(state, _fingerprint(f"FP-{run_id}"))


_ALL_STATUSES = (
    "queued", "running", "awaiting_confirmation", "awaiting_signoff",
    "completed", "failed", "interrupted",
)


@pytest.fixture
def real_ctx(monkeypatch, tmp_path):
    """One real orchestrator.service + LocalPersistence context, seeded
    with one run per status -- function-scoped, using `monkeypatch`, the
    EXACT pattern tests/test_runs_page.py's own `real_ctx` fixture uses
    (not this module's earlier module-scoped attempt, which broke: this
    directory's autouse, function-scoped `fake_backend` fixture -- conftest.py
    -- repoints `adapters.service`/`adapters._ctx` at the fake backend
    before EVERY test function, so a module-scoped fixture's one-time
    mutation of that same module-level state is silently undone before the
    second test in the module ever runs; only re-applying it per test, the
    same scope `fake_backend` itself uses, survives that)."""
    from orchestrator import service as real_service

    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(tmp_path),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
        "SKILLS_DIR": str(_REPO_ROOT / "skills"),
    }
    monkeypatch.setattr(adapters, "service", real_service)
    adapters._ctx = None
    original_build = real_service.build_app_context
    monkeypatch.setattr(real_service, "build_app_context", lambda *a, **k: original_build(env))

    ctx = adapters.get_context()
    for i, status in enumerate(_ALL_STATUSES):
        # skill_id="SKILL-001"/mode="playbook" for every status (rather than
        # mixing in an Explorer run) so /runs' has_workspace routing and
        # every run_status.py status branch are both exercised against a
        # Skill this checkout actually has (skills/tne_exco/manifest.yaml).
        _make_bare_run(ctx, f"RUN-INV-{i}", status=status, skill_id="SKILL-001", mode="playbook")
    _make_bare_run(ctx, "RUN-INV-EXPLORER", status="completed", skill_id=None, mode="explorer")

    try:
        yield ctx
    finally:
        ctx.executor.stop()
        adapters._ctx = None


@pytest.fixture(scope="module")
def app_entry():
    """The REAL, fully wired Dash app (app/app.py) -- built once, its
    `callback_map` is static (built at import time by every
    `register_callbacks(app)` call) and does not depend on which backend
    `adapters` currently points at, so this is independent of `real_ctx`."""
    return load_app_entry()


_TNE_PLANTED_DATA_DIR = _REPO_ROOT / "tests" / "fixtures" / "tne_planted" / "data"


@pytest.fixture
def real_completed_tne_run(monkeypatch, tmp_path):
    """One real, COMPLETED SKILL-001 run over the fast planted fixture
    (tests/test_no_dev_strings.py's own `_completed_skill001_export`
    fixture uses the identical data dir, for the identical reason: the real
    ~3-minute synthetic_data/ run this repo also has is far too slow for a
    test that only needs a genuine `workspace_tne`/`run_status` render, not
    a realistic finding set) -- /workspace/tne has no equivalent among
    `real_ctx`'s bare RunStates (it needs actual persisted `RF_*` flags and
    test_results, which only a real pipeline pass produces).

    Function-scoped with `monkeypatch`, not module-scoped with a manual
    save/restore, for the SAME reason `real_ctx` above is: this directory's
    autouse, function-scoped `fake_backend` fixture (conftest.py) runs
    AFTER a module-scoped fixture's one-time setup but BEFORE the test body
    (pytest sets up broader-scoped fixtures first, so a module fixture's
    direct `adapters.service = real_service` assignment would be overwritten
    by `fake_backend` moments later, every single time, including the very
    first test) -- only `monkeypatch`, the same tool `fake_backend` itself
    uses, stacks and reverts correctly regardless of ordering. Only one test
    below uses this fixture, so nothing is lost by not module-scoping it."""
    if not _TNE_PLANTED_DATA_DIR.is_dir():
        pytest.skip("tests/fixtures/tne_planted/data/ not present -- run its generate.py first")

    from orchestrator import service as real_service

    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(_TNE_PLANTED_DATA_DIR),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
        "ORCH_WORKER_ID": "control-inventory",
        "SKILLS_DIR": str(_REPO_ROOT / "skills"),
        "CODE_REVISION": "test-fixed-revision",
    }
    ctx = real_service.build_app_context(env)
    ctx.executor.start()
    # workspace_tne.tne_workspace_layout reads through `adapters.xxx(...)`
    # calls, which resolve `service`/`_ctx` off the shared `adapters`
    # module (src/platform/adapters.py's own `get_context`) -- pointing
    # both at this real ctx/service is what makes the render genuine
    # (adapters.list_findings(a FakeContext, ...) would be a type
    # mismatch, not a real render) rather than accidentally correct only
    # because some OTHER fixture ran first in this module and left them
    # set.
    monkeypatch.setattr(adapters, "service", real_service)
    adapters._ctx = ctx
    try:
        bindings = real_service.suggest_bindings(ctx, "SKILL-001")
        run_id = real_service.start_audit_run(
            ctx, skill_id="SKILL-001", bindings=bindings,
            audit_period=("2025-01-01", "2026-04-30"),
            objective="Control inventory completed-run check.", run_owner="control-inventory",
        )

        def _wait(statuses, timeout):
            deadline = time.time() + timeout
            status = None
            while time.time() < deadline:
                status = real_service.get_run(ctx, run_id)["status"]
                if status in statuses:
                    return status
                time.sleep(0.2)
            return status

        status = _wait({"awaiting_signoff", "failed"}, timeout=90)
        assert status == "awaiting_signoff", real_service.get_run(ctx, run_id).get("status_reason")
        real_service.sign_off(ctx, run_id, "control-inventory")
        status = _wait({"completed", "failed"}, timeout=30)
        assert status == "completed", real_service.get_run(ctx, run_id).get("status_reason")

        yield ctx, run_id
    finally:
        ctx.executor.stop()
        adapters._ctx = None


# ── Pages with no per-run data at all ────────────────────────────────────

def test_landing_page_controls(app_entry, real_ctx):
    page = run_setup.home_layout()
    _check_page(app_entry.app, "landing (/)", page)


def test_skill_library_page_controls(app_entry, real_ctx):
    page = skill_library_page()
    _check_page(app_entry.app, "/skills", page)


def test_skill_detail_page_controls(app_entry, real_ctx):
    page = skill_methodology_page("SKILL-001")
    _check_page(app_entry.app, "/skills/SKILL-001", page)


def test_platform_nav_controls(app_entry, real_ctx):
    """Not one of the task's named pages, but genuinely part of every real
    page render (app/app.py's serve_layout places it once, outside
    page-content) and carries six real dcc.Link controls -- left out would
    mean the app's own primary navigation is never checked at all."""
    _check_page(app_entry.app, "platform_nav", platform_nav())


# ── Pages driven by real_ctx's runs ──────────────────────────────────────

def test_runs_page_controls(app_entry, real_ctx):
    page = audit_runs_page()
    _check_page(app_entry.app, "/runs", page)


@pytest.mark.parametrize("status_index", range(len(_ALL_STATUSES)))
def test_run_page_controls_for_each_status(app_entry, real_ctx, status_index):
    status = _ALL_STATUSES[status_index]
    run_id = f"RUN-INV-{status_index}"
    run, narration = run_status._run_and_narration(run_id)
    assert run is not None and run["status"] == status, run
    # run_status.run_page(run_id) itself only returns the empty
    # `run-page-body` shell (the real content is filled in by a callback
    # fired on load/poll -- src/run_status.py's own module docstring/
    # `_render_body`) -- the SAME function that callback calls is what a
    # real browser would actually show, so that is what this test walks.
    body = run_status._render_body(run, run_id, narration)
    _check_page(app_entry.app, f"/run/{run_id} (status={status})", body)


def test_actions_page_controls(app_entry, real_ctx):
    page = management_actions_page()
    _check_page(app_entry.app, "/actions", page)


def test_trace_page_controls(app_entry, real_ctx):
    page = platform_trace_page()
    _check_page(app_entry.app, "/trace", page)


# ── /workspace/tne against a real completed run ──────────────────────────

def test_workspace_tne_page_controls(app_entry, real_completed_tne_run):
    # real_completed_tne_run already points adapters.service/_ctx at this
    # same real ctx for the duration of the fixture (see its own docstring).
    _ctx, run_id = real_completed_tne_run
    page = workspace_tne.tne_workspace_layout(run_id)
    _check_page(app_entry.app, "/workspace/tne", page)
