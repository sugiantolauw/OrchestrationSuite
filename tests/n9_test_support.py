"""Shared harness for WP N9's own tests (docs/specs/P6_narration_design.md
§5.2, §5.5, §6.4, §11: T-C6, T-R1, G14, the sign-off gate). Builds on
`tests/narration_test_support.py` (the mini Skill fixture, `DispatchingModelClient`,
`happy_responses`) rather than duplicating it, and adds the one thing that
harness does not: an `AppContext` (the `orchestrator.service` entry point WP
N9's own functions take) wired against the SAME persistence/settings/skill,
driven through the REAL state machine (`orchestrator.pipeline.run_phase`) so
`state.status` genuinely reaches `awaiting_signoff` -- not merely a
node-by-node call sequence with no CAS-backed status of its own.

Deliberately imports `orchestrator.nodes.fieldwork.NODES_FOR` directly
(the complete node list, `narrate` included) rather than
`orchestrator.nodes.registry.NODES_FOR` (what `service.py`/`executor.py`
use in production) -- see this WP's final report: the registry's
`_NODE_SPECS["fieldwork"]["execute"]` was never updated when WP N7 added
`narrate` to `nodes/fieldwork.py`'s own copy, so the registry-driven path
currently skips `narrate` entirely. That gap is orchestrator/nodes/
registry.py, out of this WP's scope to fix (`nodes/` is off-limits here);
using the fieldwork module's own complete list is what lets these tests
exercise the real `narrate` node without waiting on that fix, exactly as
`tests/test_surface1.py` and `tests/test_e2e_trivial.py` already do for
their own purposes.
"""

from __future__ import annotations

from orchestrator.nodes.fieldwork import NODES_FOR as FIELDWORK_NODES_FOR
from orchestrator.pipeline import run_phase
from orchestrator.service import AppContext
from tests.narration_test_support import (
    DispatchingModelClient,
    NarrationHarness,
    happy_responses,
    make_narration_harness,
)

__all__ = [
    "FIELDWORK_NODES_FOR",
    "app_context_for",
    "harness_at_awaiting_signoff",
]


def app_context_for(h: NarrationHarness, *, clock) -> AppContext:
    """An `AppContext` sharing the harness's persistence/settings/skills_dir/
    export_storage, for calling `orchestrator.service`'s WP N9 functions
    against the SAME run. `executor=None` (as every other direct-AppContext
    test in this suite does, e.g. tests/test_status.py's callers) -- these
    tests drive the pipeline explicitly via `run_phase`, never through a
    background executor."""
    return AppContext(
        settings=h.ctx.settings,
        persistence=h.persistence,
        skills_dir=h.ctx.skill.skill_dir.parent,
        data_source_factory=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("data_source_factory should not be called by any P6 WP N9 service function")
        ),
        export_storage=h.ctx.export_storage,
        clock=clock,
        executor=None,
    )


def harness_at_awaiting_signoff(local_persistence, tmp_path, clock, *, model_client=None):
    """Runs the mini Skill fixture all the way from `queued` to a genuine,
    state-machine-reached `awaiting_signoff` -- `plan` (auto-confirmed),
    then `execute` including the real `narrate` node, in one `run_phase`
    call (CLAUDE.md §2.4/§4.2's amended fieldwork order). Returns
    `(harness, ctx_app, state)`."""
    client = model_client if model_client is not None else DispatchingModelClient(happy_responses())
    h = make_narration_harness(local_persistence, tmp_path, model_client=client)
    fingerprint = h.persistence.get_fingerprint(h.state.fingerprint_id)
    state = run_phase(
        h.persistence, h.state.run_id, nodes_for=FIELDWORK_NODES_FOR, skill=h.ctx, clock=clock,
        current_fingerprint=fingerprint,
    )
    assert state.status == "awaiting_signoff", state.status_reason
    ctx_app = app_context_for(h, clock=clock)
    return h, ctx_app, state
