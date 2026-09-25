"""BUG-2.4-LIVETEST-CTX regression (offline, always runs -- no RUN_LIVE_LLM
needed). tests/live/test_narration_live.py's own post-hoc re-validation
step called service._narrative_table(h.ctx, ...)/_narrative_allowed_identifiers
with `h.ctx`, a bare orchestrator.nodes.context.NodeContext (what `narrate()`
itself takes) -- but both functions resolve the run's Skill via
resolve_run_skill -> load_skill_by_id -> _skill_dir_for(ctx.skills_dir), a
field only orchestrator.service.AppContext carries. Every call crashed with
AttributeError: 'NodeContext' object has no attribute 'skills_dir' -- this
was never caught before because it was the first live run of that
replacement code (commit 8a78f6a's own message). The fix is test-harness
only: tests/live/test_narration_live.py now builds an AppContext via
tests/n9_test_support.py's app_context_for(h, ...) (the same helper WP N9's
own tests already use) instead of passing `h.ctx` straight through. This
test proves both halves offline: the AttributeError reproduces on the bare
NodeContext, and app_context_for(h, ...) fixes it."""

from __future__ import annotations

import pytest

from orchestrator import service
from orchestrator.adapters.persistence_local import LocalPersistence
from orchestrator.nodes.narration import narrate
from tests.n9_test_support import app_context_for
from tests.narration_test_support import (
    DispatchingModelClient,
    happy_responses,
    make_narration_harness,
    run_to_narrate_input,
)


def test_narrative_table_helpers_need_an_appcontext_not_a_bare_nodecontext(tmp_path):
    persistence = LocalPersistence(str(tmp_path / "orch.db"))
    persistence.migrate()
    h = make_narration_harness(
        persistence, tmp_path, model_client=DispatchingModelClient(happy_responses()),
    )
    state = run_to_narrate_input(h)
    result = narrate(h.ctx, state)

    narratives = persistence.get_narratives(state.run_id)
    # "finding"/"theme" targets are the ones that call resolve_run_skill
    # (CLAUDE.md service.py:2754/2772) -- "candidate"/"run"/"profile"/"chart"
    # targets don't always need a Skill lookup, so pick a finding row
    # specifically to reproduce the exact crash the live test hit.
    finding_rows = [
        n for n in narratives
        if n["origin"] in ("model", "model_repaired") and n["target_kind"] == "finding"
    ]
    assert finding_rows

    row = finding_rows[0]
    # The bare NodeContext narrate() itself ran on has no skills_dir --
    # reproduces the exact crash the live test hit (the failing line was
    # `service._narrative_table(h.ctx, result, n)`, tests/live/
    # test_narration_live.py:83 before this fix).
    assert not hasattr(h.ctx, "skills_dir")
    with pytest.raises(AttributeError):
        service._narrative_table(h.ctx, result, row)

    # app_context_for(h, ...) -- what tests/live/test_narration_live.py now
    # uses -- carries skills_dir, and both calls succeed for every target
    # kind resolve_run_skill can be reached from (finding, and candidate
    # via _narrative_allowed_identifiers).
    app_ctx = app_context_for(h, clock=h.ctx.clock)
    table = service._narrative_table(app_ctx, result, row)
    assert isinstance(table, dict) and table

    allowed_identifiers = service._narrative_allowed_identifiers(app_ctx, result, row)
    assert isinstance(allowed_identifiers, frozenset)
