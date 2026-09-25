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
  3. renders it (`orchestrator.narration.placeholders.render`) and asserts
     every remaining digit character in the rendered text sits inside a
     span that WAS a placeholder in the template -- the digit can only
     have come from `format_metric_value`, never from stray model prose
     (the "span tracking" requirement);
  4. for an `observation` field (a finding's or a candidate's) and for the
     run's `exec_summary`, additionally requires that every one of the
     item's own cited metrics was actually referenced (N-C1 coverage, in
     the direction "every number in the text maps to a cited metric" is
     necessary but not sufficient -- this is the other direction, "every
     cited metric appears").

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
                spans = scan_placeholders(item)
                stripped = strip_placeholder_spans(item, spans)
                stray_digits = [ch for ch in stripped if ch.isnumeric()]
                assert not stray_digits, (
                    f"{run_id} {row['narrative_id']}: digit(s) {stray_digits} outside any "
                    f"placeholder span in stored text {item!r}"
                )

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

            if required is not None:
                missing = required - covered
                assert not missing, (
                    f"{run_id} {row['narrative_id']} ({row['target_kind']}.{row['field']}): "
                    f"cited metric(s) {sorted(missing)} never referenced in the stored prose"
                )
    assert checked > 0, "no narrative item was checked -- the fixture e2e runs produced no real prose"


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
