"""No developer-facing string (CLAUDE.md, section markers, gate/phase names,
code identifiers) ever reaches rendered UI text. Renders every route with
the fake service (no live backend needed) and, where a completed local run
is easy to construct, with the real local-backend service on that run."""

from __future__ import annotations

import re

import pytest

_FORBIDDEN_SUBSTRINGS = [
    "CLAUDE.md",
    "§",
    "NN13", "NN14", "NN15", "NN16",
    "G6", "G7", "G8", "G9", "G10", "G11", "G12", "G13", "G14", "G15", "G16", "G17",
]
_FORBIDDEN_PATTERNS = [re.compile(r"\bP[0-9][AB]?\b")]  # phase names: P1A, P1B, P2 .. P9


def _all_text(node, out):
    if node is None:
        return
    if isinstance(node, (str, int, float)):
        out.append(str(node))
        return
    if isinstance(node, (list, tuple)):
        for n in node:
            _all_text(n, out)
        return
    children = getattr(node, "children", None)
    if children is not None:
        _all_text(children, out)
    # dcc.Markdown / dcc.Textarea etc. carry text in `value`/other props too.
    for prop in ("value", "placeholder", "title", "label"):
        val = getattr(node, prop, None)
        if isinstance(val, str):
            out.append(val)


def render_text(component) -> str:
    out: list[str] = []
    _all_text(component, out)
    return "\n".join(out)


def assert_no_dev_strings(component, label: str) -> None:
    text = render_text(component)
    for token in _FORBIDDEN_SUBSTRINGS:
        assert token not in text, f"{label}: found developer string {token!r} in rendered text"
    for pat in _FORBIDDEN_PATTERNS:
        m = pat.search(text)
        assert m is None, f"{label}: found developer string {m.group(0)!r} in rendered text"


@pytest.fixture(autouse=True)
def _fake_backend(monkeypatch):
    import tests.fake_service as fake_service
    from src.platform import adapters

    ctx = fake_service.build_app_context()
    monkeypatch.setattr(adapters, "service", fake_service, raising=False)
    monkeypatch.setattr(adapters, "_ctx", ctx, raising=False)
    monkeypatch.setattr(adapters, "get_context", lambda: ctx, raising=False)
    run_id = fake_service.start_audit_run(
        ctx, skill_id="SKILL-001", bindings={}, audit_period=("2025-01-01", "2025-06-30"),
        objective="Test objective", run_owner="tester", review_plan_first=False,
    )
    fake_service.sign_off(ctx, run_id, "tester")
    return run_id


def test_landing_page_has_no_dev_strings():
    from src.run_setup import home_layout

    assert_no_dev_strings(home_layout(), "/")


def test_skill_library_page_has_no_dev_strings():
    from src.platform.pages import skill_library_page

    assert_no_dev_strings(skill_library_page(), "/skills")


def test_skill_methodology_page_has_no_dev_strings():
    from src.platform.pages import skill_methodology_page

    assert_no_dev_strings(skill_methodology_page("SKILL-001"), "/skills/SKILL-001")


def test_audit_runs_page_has_no_dev_strings():
    from src.platform.pages import audit_runs_page

    assert_no_dev_strings(audit_runs_page(), "/runs")


def test_platform_trace_page_has_no_dev_strings():
    from src.platform.pages import platform_trace_page

    assert_no_dev_strings(platform_trace_page(), "/trace")


def test_management_actions_page_has_no_dev_strings():
    from src.platform.pages import management_actions_page

    assert_no_dev_strings(management_actions_page(), "/actions")


def test_run_status_page_has_no_dev_strings(_fake_backend):
    from src.run_status import run_page, _render_body
    from src.platform import adapters

    run = adapters.get_run(_fake_backend)
    assert_no_dev_strings(run_page(_fake_backend), "/run/<id> (shell)")
    assert_no_dev_strings(_render_body(run, _fake_backend), "/run/<id> (body)")


def test_workspace_tne_has_no_dev_strings(_fake_backend):
    from src.workspace_tne import tne_workspace_layout

    assert_no_dev_strings(tne_workspace_layout(_fake_backend), "/workspace/tne")
