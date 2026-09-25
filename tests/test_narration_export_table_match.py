"""BUG-4c (independent narration-content review 2026-09-25, found live: the
exec summary and the 'risk_and_exposure' chart caption both came back
`render_error`/"model text failed validation" on a real GPT-OSS run --
neither is a genuine content violation). Same shape as the already-fixed
BUG-4b (`tests/test_g11_invariant.py`'s own `service._narrative_table`
"chart" case): `orchestrator.narration.run_values.run_values` and
`orchestrator.nodes.narration._chart_specs` may add
`run_exposure_dominant_amount`/`_title`/`_basis` to the table a model is
shown at GENERATION time (`build_run_table`/the risk_and_exposure chart's
own metric_names) -- but until this fix,
`orchestrator.nodes.fieldwork._build_export_narration` (the XLSX/PPTX
export's own re-validation table) rebuilt the exec-summary table with the
bare `run_values()` and the caption table with a single-metric
`metrics_placeholder_table(["run_exposure_headline"], ...)`, neither of
which carries those extra names. A model that legitimately used one (as
the prompts now explicitly ask it to, §3.2/§4.6) then hit `render()`
raising `NarrationConfigError` on its own already-validated text, and the
export silently substituted "model text failed validation" for a
perfectly good paragraph/caption. This test drives a real `narrate()` +
`export()` pass with a model response that cites
`{money:run_exposure_dominant_amount}`/`{value:run_exposure_dominant_title}`
and asserts BOTH the exec summary and the caption resolve with their real
model text -- never `render_error`."""

from __future__ import annotations

from orchestrator.nodes.fieldwork import _build_export_narration
from orchestrator.nodes.narration import narrate
from tests.narration_test_support import (
    MARKER_CAPTIONS,
    MARKER_EXEC_SUMMARY,
    DispatchingModelClient,
    happy_responses,
    make_narration_harness,
    resp,
    run_to_narrate_input,
)


def test_exec_summary_and_caption_citing_the_dominant_exposure_placeholder_do_not_render_error(
    local_persistence, tmp_path,
):
    responses = dict(happy_responses())
    responses[MARKER_EXEC_SUMMARY] = resp(
        {
            "schema_version": "exec-summary/1",
            "paragraphs": [
                # Round-6 narration-content review, item 2 (N-C1 on
                # exec_summary): this mini Skill's own T1 always resolves
                # High, so `run_values` always declares a
                # `run_high_finding_1_title` entry -- cite it too, or the
                # coverage check now fails this fixture's response.
                "This run raised {count:run_finding_count} finding(s), an amount at risk of "
                "{money:run_exposure_headline}, including the High-severity finding "
                "{value:run_high_finding_1_title}.",
                "That figure is driven by {value:run_exposure_dominant_title}, contributing "
                "{money:run_exposure_dominant_amount}.",
            ],
        }
    )
    responses[MARKER_CAPTIONS] = resp(
        {
            "schema_version": "chart-captions/1",
            "captions": [
                {
                    "chart_id": "severity_distribution",
                    "caption": "This chart shows the count of findings at each severity level for this run.",
                },
                {
                    "chart_id": "risk_and_exposure",
                    "caption": (
                        "This chart shows the run's amount at risk, driven mainly by "
                        "{value:run_exposure_dominant_title} at {money:run_exposure_dominant_amount}."
                    ),
                },
            ],
        }
    )
    client = DispatchingModelClient(responses)
    h = make_narration_harness(local_persistence, tmp_path, model_client=client)
    state = run_to_narrate_input(h)
    state = narrate(h.ctx, state)

    findings = local_persistence.list_findings(state.run_id)
    metrics = local_persistence.get_run_metrics(state.run_id)
    narration = _build_export_narration(h.ctx, state, findings, metrics)

    assert narration["exec_summary_label"] != "Deterministic output — model text failed validation"
    assert narration["exec_summary_paragraphs"] is not None
    assert any("driven by" in p for p in narration["exec_summary_paragraphs"])

    assert narration["risk_chart_caption"] is not None
    assert "driven mainly by" in narration["risk_chart_caption"]

    row_by_field = {
        (r["target_kind"], r["target_id"], r["field"]): r for r in narration["narrative_rows"]
    }
    exec_row = row_by_field.get(("run", "run", "exec_summary"))
    assert exec_row is not None
    assert exec_row["status"] in ("model", "model_repaired")

    caption_row = row_by_field.get(("chart", "risk_and_exposure", "caption"))
    assert caption_row is not None
    assert caption_row["status"] in ("model", "model_repaired")
