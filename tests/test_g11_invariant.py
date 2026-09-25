"""G11 stored-state invariant (P6 WP N13, docs/specs/P6_narration_design.md
§3.6 point 2, §11 T-G11I): "After every e2e run (local backend), every
stored model narrative re-validates green against its stored table, and
its rendered text equals render(template_text, table)."

Runs every scenario tests/test_narration_e2e_local.py already drives
through the real state machine (the happy path with an accepted candidate
and a human edit, the degraded-Sonnet run, and the G10 clean run), then
independently re-reads every persisted `narratives` row from Delta/SQLite
and, for each one whose origin means it carries real prose (`model`,
`model_repaired` or `human_edit` -- the two fallback origins persist
`template_text=None`, nothing to check):

  1. rebuilds "its stored table" the SAME way orchestrator.service's own
     edit-validation path does (`service._narrative_table`, itself built
     from this run's CURRENT persisted findings/candidates/themes/metrics
     -- never a re-run through a model);
  2. re-validates the stored `template_text` against that table with the
     SAME public validator (`orchestrator.narration.validate.validate_prose`)
     the generation-time code path used, under the row's own origin (never
     a `model`-origin re-check against a `human_edit` row's legitimately
     typed digits);
  3. for a `model`/`model_repaired` row only (docs/specs/P6_narration_design.md
     §3.6 point 2's own wording, "every stored MODEL narrative"), renders it
     (`orchestrator.narration.placeholders.render`) and asserts every
     remaining digit character in the rendered text sits inside a span that
     WAS a placeholder in the template -- the digit can only have come from
     `format_metric_value`, never from stray model prose (the "span
     tracking" requirement). A `human_edit` row is exempt: N-H1 (already
     checked in step 2) is its correct, by-design invariant -- a literal
     typed digit that equals a cited metric's rendering, never routed
     through a placeholder at all;
  4. for a `model`/`model_repaired` `observation` field (a finding's or a
     candidate's) and for the run's `exec_summary`, additionally requires
     that every one of the item's own cited metrics was actually referenced
     (N-C1 coverage, in the direction "every number in the text maps to a
     cited metric" is necessary but not sufficient -- this is the other
     direction, "every cited metric appears"). Also exempt for `human_edit`:
     an auditor may restate a cited figure as a literal number without using
     its placeholder name.

This is independent of, and never reuses, the generation-time validation
inside orchestrator.narration.runner/candidates -- it re-derives the table
and re-validates the text fresh from what is actually sitting in the
persistence layer after the run completed."""

from __future__ import annotations

import json

import pytest

from orchestrator import service
from orchestrator.narration.placeholders import render, scan_placeholders, strip_placeholder_spans
from orchestrator.narration.validate import validate_prose
from tests.narration_test_support import resp
from tests.test_narration_e2e_local import (
    MARKER_CAPTIONS,
    _build_ctx,
    _happy_client,
    _start_run,
    _wait_for,
    _write_clean_data,
)

_REAL_PROSE_ORIGINS = ("model", "model_repaired", "human_edit")


def _run_happy_path(tmp_path) -> tuple[service.AppContext, str]:
    ctx = _build_ctx(tmp_path)
    ctx.executor.start()
    ctx.model_client = _happy_client()
    run_id = _start_run(ctx)
    status = _wait_for(ctx, run_id, {"awaiting_signoff", "failed"})
    assert status == "awaiting_signoff", service.get_run(ctx, run_id).get("status_reason")

    candidates = ctx.persistence.list_candidates(run_id)
    accepted = next(c for c in candidates if c["title"] == "Missing Register Match Amount")
    rejected = next(c for c in candidates if c["title"] != "Missing Register Match Amount")
    service.decide_candidate(
        ctx, run_id, accepted["candidate_id"], decision="accepted", decided_severity="Medium", actor="alice",
    )
    service.decide_candidate(
        ctx, run_id, rejected["candidate_id"], decision="rejected", reason="scope.", actor="alice",
    )

    state = ctx.persistence.load_state(run_id)
    findings = ctx.persistence.list_findings(run_id)
    t1_finding_id = next(f["finding_id"] for f in findings if f.get("test_id") == "T1")
    t1_observation_id = state.finding_narratives[t1_finding_id]
    service.edit_narrative(
        ctx, run_id, t1_observation_id,
        "In this run, {count:hv_count} claim(s) exceeded the high-value threshold, "
        "worth {money:hv_amount} in total exposure.",
        actor="alice",
    )

    service.sign_off(ctx, run_id, "alice")
    status = _wait_for(ctx, run_id, {"completed", "failed"})
    assert status == "completed", service.get_run(ctx, run_id).get("status_reason")
    return ctx, run_id


def _run_degraded(tmp_path) -> tuple[service.AppContext, str]:
    from orchestrator.adapters.model_fake import RaisingModelClient
    from orchestrator.llm.errors import ModelUnavailable
    from tests.narration_test_support import MODEL_SONNET_ENDPOINT

    ctx = _build_ctx(tmp_path, model_sonnet=None)
    ctx.executor.start()
    ctx.model_client = RaisingModelClient(ModelUnavailable(MODEL_SONNET_ENDPOINT, "rate limit 0", permanent=True))
    run_id = _start_run(ctx)
    status = _wait_for(ctx, run_id, {"awaiting_signoff", "failed"})
    assert status == "awaiting_signoff", service.get_run(ctx, run_id).get("status_reason")
    service.sign_off(ctx, run_id, "alice")
    status = _wait_for(ctx, run_id, {"completed", "failed"})
    assert status == "completed", service.get_run(ctx, run_id).get("status_reason")
    return ctx, run_id


def _run_g10_clean(tmp_path) -> tuple[service.AppContext, str]:
    ctx = _build_ctx(tmp_path, data_writer=_write_clean_data)
    ctx.executor.start()
    ctx.model_client = _happy_client()
    run_id = _start_run(ctx)
    status = _wait_for(ctx, run_id, {"awaiting_signoff", "failed"})
    assert status == "awaiting_signoff", service.get_run(ctx, run_id).get("status_reason")
    service.sign_off(ctx, run_id, "alice")
    status = _wait_for(ctx, run_id, {"completed", "failed"})
    assert status == "completed", service.get_run(ctx, run_id).get("status_reason")
    return ctx, run_id


@pytest.fixture(scope="module")
def e2e_runs(tmp_path_factory):
    runs = [
        _run_happy_path(tmp_path_factory.mktemp("g11-happy")),
        _run_degraded(tmp_path_factory.mktemp("g11-degraded")),
        _run_g10_clean(tmp_path_factory.mktemp("g11-clean")),
    ]
    try:
        yield runs
    finally:
        for ctx, _ in runs:
            ctx.executor.stop()


def _required_coverage_names(ctx: service.AppContext, state, row: dict, table: dict) -> frozenset[str] | None:
    """The names an `observation`/`exec_summary` row must reference at
    least once (N-C1's "every cited metric appears" direction, §3.2 of the
    design) -- None for every field that carries no coverage requirement."""
    target_kind, field = row["target_kind"], row["field"]
    if target_kind in ("finding", "candidate") and field == "observation":
        if target_kind == "finding":
            item = next(f for f in ctx.persistence.list_findings(state.run_id) if f["finding_id"] == row["target_id"])
        else:
            item = next(c for c in ctx.persistence.list_candidates(state.run_id) if c["candidate_id"] == row["target_id"])
        cited = item.get("metrics_cited") or []
        if isinstance(cited, dict):
            cited = list(cited)
        return frozenset(name for name in cited if name in table)
    return None


def test_every_stored_narrative_re_validates_and_renders_with_digits_confined_to_placeholders(e2e_runs):
    checked = 0
    for ctx, run_id in e2e_runs:
        state = ctx.persistence.load_state(run_id)
        narratives = ctx.persistence.get_narratives(run_id)
        assert narratives, f"{run_id}: no narratives persisted -- nothing for this invariant to check"
        for row in narratives:
            if row["origin"] not in _REAL_PROSE_ORIGINS:
                assert row["template_text"] is None, (
                    f"{row['narrative_id']}: origin {row['origin']!r} but template_text is set -- a "
                    "fallback row must never carry stored prose"
                )
                continue

            table = service._narrative_table(ctx, state, row)
            allowed_identifiers = service._narrative_allowed_identifiers(ctx, state, row)
            validator_field = service._VALIDATOR_FIELD_FOR[(row["target_kind"], row["field"])]
            is_list = row["field"] in service._LIST_NARRATIVE_FIELDS
            raw = row["template_text"]
            items = json.loads(raw) if is_list else [raw]

            required = _required_coverage_names(ctx, state, row, table)
            origin_arg = "model" if row["origin"] in ("model", "model_repaired") else "human_edit"

            covered: set[str] = set()
            for item in items:
                checked += 1
                result = validate_prose(
                    item, table, field=validator_field, origin=origin_arg,
                    allowed_identifiers=allowed_identifiers,
                )
                assert result.valid, (
                    f"{run_id} {row['narrative_id']} ({row['target_kind']}.{row['field']}, "
                    f"origin={row['origin']}): stored prose no longer re-validates: "
                    f"{[(v.rule_id, v.message) for v in result.violations]}"
                )
                covered |= result.used_placeholders

                # Every remaining digit in the RENDERED text lies inside a
                # placeholder-derived span. Equivalently (since render()
                # leaves every non-placeholder character byte-for-byte
                # unchanged): the template with every placeholder span
                # removed has no digit character left in it at all.
                #
                # §3.6 point 2's own wording is "every stored MODEL
                # narrative" -- this span-tracking invariant is a MODEL
                # authorship guarantee (N-D1: the model writes placeholders,
                # never numbers). A human_edit row is explicitly exempt: §14
                # Answers Q3 / N-H1 (already re-checked above via
                # validate_prose(..., origin="human_edit", ...)) lets an
                # auditor type a literal digit as long as it equals a cited
                # metric's rendering -- BUG-SYNTH-BARE-TESTID-STILL-LIVE
                # (independent review round 4, 2026-09-25): this assertion
                # ran unconditionally and would fail on any genuine,
                # by-design literal-number human edit (the shared e2e
                # fixture's own edit scenario happened to use placeholder
                # syntax, so it never tripped this gap until a real run's
                # literal-text edit did).
                if origin_arg == "model":
                    spans = scan_placeholders(item)
                    stripped = strip_placeholder_spans(item, spans)
                    stray_digits = [ch for ch in stripped if ch.isnumeric()]
                    assert not stray_digits, (
                        f"{run_id} {row['narrative_id']}: digit(s) {stray_digits} outside any "
                        f"placeholder span in stored text {item!r}"
                    )
                else:
                    spans = scan_placeholders(item)

                rendered = render(item, table)
                # render() raises NarrationConfigError for any placeholder
                # this table cannot resolve -- reaching this line at all is
                # part of the assertion. Independently: every placeholder's
                # rendered numeric value must appear in the output (proves
                # substitution actually happened, not merely that it didn't
                # raise).
                for span in spans:
                    if span.valid_syntax and span.name in table and table[span.name].value is not None:
                        from orchestrator.findings import format_metric_value

                        entry = table[span.name]
                        assert format_metric_value(entry.value, entry.unit) in rendered, (
                            f"{run_id} {row['narrative_id']}: rendered value for {span.name!r} "
                            f"missing from rendered text {rendered!r}"
                        )

            # N-C1 coverage ("every cited metric appears") is a placeholder-
            # usage requirement -- meaningless for a human_edit row, which
            # may legitimately restate a cited figure as a literal number
            # (N-H1, checked above) without using that metric's placeholder
            # name at all. Scoped to origin=="model" for the same reason as
            # the span-tracking check above.
            if origin_arg == "model" and required is not None:
                missing = required - covered
                assert not missing, (
                    f"{run_id} {row['narrative_id']} ({row['target_kind']}.{row['field']}): "
                    f"cited metric(s) {sorted(missing)} never referenced in the stored prose"
                )
    assert checked > 0, "no narrative item was checked -- the fixture e2e runs produced no real prose"


def test_g11_invariant_exempts_a_literal_digit_human_edit_from_the_model_only_checks():
    """BUG-SYNTH-BARE-TESTID-STILL-LIVE (independent review round 4,
    2026-09-25): a real run (RUN-6A95B7FDD22F) had a genuine, deliberate
    human edit on a finding's observation replacing its placeholder syntax
    with a literal rendering ("198 of 234 ... 84.6%", exactly the shape
    N-H1/CLAUDE.md §14 Answers Q3 permits: a typed number equal to a cited
    metric's displayed rendering). The round's own G11 check flagged it as
    a "stray digit leak" and "required metrics not referenced" -- but those
    are placeholder-usage invariants that only apply to MODEL-authored text
    (docs/specs/P6_narration_design.md §3.6 point 2: "every stored MODEL
    narrative"). `tests/test_g11_invariant.py`'s own official stored-state
    check applied them unconditionally, to human_edit rows too -- a real
    gap that the shared e2e fixture's own edit scenario never exercised,
    because it happens to use placeholder syntax rather than a literal
    number. This reproduces the exact shape with a fresh run and proves:
    (1) the literal-digit human edit is accepted (N-H1) and persists as
    such; (2) it fails the OLD, unconditional span-tracking/coverage
    checks (proving this is a real gap, not a hypothetical one);
    (3) it passes the fixed, origin-scoped invariant this file now applies."""
    import tempfile
    from pathlib import Path

    from orchestrator.findings import format_metric_value

    with tempfile.TemporaryDirectory() as tmp:
        # A fresh run of our own (not _run_happy_path's, which edits and
        # signs off -- edit_narrative is only allowed while
        # awaiting_signoff, CLAUDE.md §6.4).
        ctx = _build_ctx(Path(tmp))
        ctx.executor.start()
        ctx.model_client = _happy_client()
        run_id = _start_run(ctx)
        status = _wait_for(ctx, run_id, {"awaiting_signoff", "failed"})
        assert status == "awaiting_signoff", service.get_run(ctx, run_id).get("status_reason")
        try:
            state = ctx.persistence.load_state(run_id)
            findings = ctx.persistence.list_findings(run_id)
            t1 = next(f for f in findings if f.get("test_id") == "T1")
            observation_id = state.finding_narratives[t1["finding_id"]]

            row = next(r for r in ctx.persistence.get_narratives(run_id) if r["narrative_id"] == observation_id)
            table = service._narrative_table(ctx, state, row)
            hv_count_rendered = format_metric_value(table["hv_count"].value, table["hv_count"].unit)
            hv_amount_rendered = format_metric_value(table["hv_amount"].value, table["hv_amount"].unit)

            literal_text = (
                f"[Regression: literal-digit human edit, no placeholders.] "
                f"{hv_count_rendered} claim(s) exceeded the high-value threshold, "
                f"worth {hv_amount_rendered} in total exposure."
            )
            # This must succeed -- N-H1 permits a typed number equal to a
            # cited metric's rendering, exactly what the round-4 finding did.
            service.edit_narrative(ctx, run_id, observation_id, literal_text, actor="bob")

            row = next(
                r for r in ctx.persistence.get_narratives(run_id) if r["narrative_id"] == observation_id
            )
            assert row["origin"] == "human_edit"
            assert row["template_text"] == literal_text

            table = service._narrative_table(ctx, state, row)
            allowed_identifiers = service._narrative_allowed_identifiers(ctx, state, row)
            validator_field = service._VALIDATOR_FIELD_FOR[(row["target_kind"], row["field"])]

            # (1) N-H1 itself: always the correct check for human_edit, and
            # it passes -- this was never the broken part.
            result = validate_prose(
                literal_text, table, field=validator_field, origin="human_edit",
                allowed_identifiers=allowed_identifiers,
            )
            assert result.valid, [(v.rule_id, v.message) for v in result.violations]

            # (2) Proves the gap is real: the OLD, origin-blind span-tracking
            # check would have failed this genuinely valid human edit.
            spans = scan_placeholders(literal_text)
            stripped = strip_placeholder_spans(literal_text, spans)
            stray_digits = [ch for ch in stripped if ch.isnumeric()]
            assert stray_digits, "expected literal digits outside any placeholder span (that's the point of the edit)"

            required = _required_coverage_names(ctx, state, row, table)
            assert required, "expected T1's observation to cite at least one metric"
            assert not (required & result.used_placeholders), (
                "expected zero placeholder usage in a literal-text edit -- coverage cannot be met by name"
            )

            # (3) The fixed invariant (this file's own test, above) does NOT
            # raise on this row -- confirmed by construction: it only
            # applies the span-tracking/coverage checks when
            # origin_arg == "model", and this row's origin is "human_edit".
        finally:
            ctx.executor.stop()


# ── BUG-4b (independent review, 2026-09-25): a chart caption that
# legitimately cites the 'severity_distribution' chart's own
# run_high_count/run_medium_count/run_low_count placeholders -- computed ad
# hoc by orchestrator.nodes.narration._chart_specs and never persisted to
# run_metrics -- must still re-validate/render, because those are exactly
# the names orchestrator.narration.payloads.build_caption_payload put in
# THIS chart_id's own table at generation time. Not part of `e2e_runs`
# above because the shared happy-path fixture's own recorded caption
# carries no placeholders at all (never exercises this branch) -- a
# dedicated run scripts a caption response that does. ──────────────────────


def test_chart_caption_citing_severity_counts_re_validates(tmp_path):
    ctx = _build_ctx(tmp_path)
    ctx.executor.start()
    try:
        client = _happy_client()
        client.by_marker[MARKER_CAPTIONS] = resp(
            {
                "schema_version": "chart-captions/1",
                "captions": [
                    {
                        "chart_id": "severity_distribution",
                        "caption": (
                            "This run raised {count:run_high_count} High, {count:run_medium_count} "
                            "Medium and {count:run_low_count} Low severity finding(s)."
                        ),
                    },
                    {
                        "chart_id": "risk_and_exposure",
                        "caption": "This chart shows the amount at risk identified by this run.",
                    },
                ],
            },
            model="gpt-oss-test-v1",
        )
        ctx.model_client = client
        run_id = _start_run(ctx)
        status = _wait_for(ctx, run_id, {"awaiting_signoff", "failed"})
        assert status == "awaiting_signoff", service.get_run(ctx, run_id).get("status_reason")

        state = ctx.persistence.load_state(run_id)
        caption_row = next(
            r for r in ctx.persistence.get_narratives(run_id)
            if r["target_kind"] == "chart" and r["target_id"] == "severity_distribution" and r["field"] == "caption"
        )
        assert caption_row["origin"] == "model", (
            "the scripted caption must generate/validate cleanly at generation time, or this test "
            "proves nothing about the READ-time table matching it"
        )

        table = service._narrative_table(ctx, state, caption_row)
        for name in ("run_high_count", "run_medium_count", "run_low_count"):
            assert name in table, f"{name!r} missing from the re-derived chart table (BUG-4b)"

        rendered = render(caption_row["template_text"], table)  # must not raise NarrationConfigError
        assert "{count:run_low_count}" not in rendered

        result = validate_prose(
            caption_row["template_text"], table, field=service._VALIDATOR_FIELD_FOR[("chart", "caption")],
            origin="model",
        )
        assert result.valid, [(v.rule_id, v.message) for v in result.violations]
    finally:
        ctx.executor.stop()
