"""orchestrator/pptx_export.py (CLAUDE.md §4.7): the bounded slide-count
rule, measured-truncation ellipsis, no-shape-overflow geometry check, the
zero-findings path, and G13-PPTX (every number a rendered deck shows equals
a persisted value, same rounding) -- the PPTX analogue of
tests/test_p3_tne_gates.py's G13 XLSX test, against the same fast planted
fixture."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pytest
from pptx import Presentation

from orchestrator.config import DEFAULT_PPTX_TEMPLATE_PATH
from orchestrator.nodes.fieldwork import (
    _code_revision_export_note, act, classify, discover, execute, export, find, prioritise,
)
from orchestrator.pptx_export import (
    ALL_FINDINGS_ROWS_PER_SLIDE,
    COVERAGE_ROWS_PER_SLIDE,
    TOP_MATTERS_MAX,
    _ellipsize,
    _money0,
    expected_slide_count,
    generate_pptx,
    load_catalogue_rows,
)
from tests.test_p3_tne_gates import DATA_DIR, SKILL_DIR, _make_ctx_and_state

pytestmark = pytest.mark.skipif(
    not DATA_DIR.is_dir(), reason="tests/fixtures/tne_planted/data/ not present -- run generate.py first"
)


# ── expected_slide_count() -- pure, no I/O ──────────────────────────────────


def test_expected_slide_count_zero_findings():
    n_catalogue = 14
    # cover + exec summary + what-we-found + 1 "no exceptions" slide +
    # risk/exposure + 1 all-findings("no findings") + 1 coverage + methodology + end
    assert expected_slide_count(0, n_catalogue) == 9


def test_expected_slide_count_caps_top_matters_at_five():
    # Held under ALL_FINDINGS_ROWS_PER_SLIDE so the appendix table's own
    # chunk count stays fixed at 1 slide either way -- isolating the
    # Top Matters cap (never more than 5 slides) from the appendix's
    # separate, independent chunking rule (covered on its own below).
    assert TOP_MATTERS_MAX + 3 <= ALL_FINDINGS_ROWS_PER_SLIDE
    n_catalogue = 14
    assert expected_slide_count(TOP_MATTERS_MAX, n_catalogue) == expected_slide_count(
        TOP_MATTERS_MAX + 3, n_catalogue
    )


def test_expected_slide_count_chunks_all_findings_and_coverage_tables():
    base = 7  # cover, exec summary, what-we-found, top-matters(5 max), risk/exposure, methodology, end
    for n_findings in (1, ALL_FINDINGS_ROWS_PER_SLIDE, ALL_FINDINGS_ROWS_PER_SLIDE + 1, 50):
        for n_catalogue in (1, COVERAGE_ROWS_PER_SLIDE, COVERAGE_ROWS_PER_SLIDE + 1):
            top_matters = min(TOP_MATTERS_MAX, n_findings)
            all_findings_slides = math.ceil(n_findings / ALL_FINDINGS_ROWS_PER_SLIDE)
            coverage_slides = math.ceil(n_catalogue / COVERAGE_ROWS_PER_SLIDE)
            expected = base - 1 + top_matters + all_findings_slides + coverage_slides
            assert expected_slide_count(n_findings, n_catalogue) == expected, (n_findings, n_catalogue)


# ── _ellipsize() -- measured truncation with an explicit ellipsis ──────────


def test_ellipsize_leaves_short_text_untouched():
    assert _ellipsize("short text", 100) == "short text"


def test_ellipsize_truncates_long_text_with_explicit_ellipsis():
    long_text = "x" * 1000
    result = _ellipsize(long_text, 50)
    assert len(result) == 50
    assert result.endswith("…")
    assert result[:-1] == "x" * 49


def test_ellipsize_collapses_whitespace_before_measuring():
    assert _ellipsize("a   b\n\nc", 100) == "a b c"


# ── a real generated deck: geometry, robustness, G13 ────────────────────────


@pytest.fixture(scope="module")
def real_deck():
    """One completed run over the planted fixture (executed once, reused by
    every test below), plus its rendered PPTX and the persisted values it
    must match."""
    from tests.conftest import canonical_ts

    from orchestrator.adapters.persistence_local import LocalPersistence

    persistence = LocalPersistence(":memory:")
    persistence.migrate()
    ctx, state = _make_ctx_and_state(persistence, DATA_DIR, run_id="RUN-PPTX-G13")
    state = discover(ctx, state)
    state = execute(ctx, state)
    state = classify(ctx, state)
    state = find(ctx, state)
    state = prioritise(ctx, state)
    state = act(ctx, state)
    state = export(ctx, state)

    findings = persistence.list_findings(state.run_id)
    metrics = persistence.get_run_metrics(state.run_id)
    assert findings, "no findings fired on this fixture -- test is not exercising anything"

    pptx_meta = state.exports["pptx"]
    content = ctx.export_storage.read(pptx_meta["path"])
    assert hashlib.sha256(content).hexdigest() == pptx_meta["sha256"]
    import io

    prs = Presentation(io.BytesIO(content))
    return {"ctx": ctx, "state": state, "findings": findings, "metrics": metrics, "prs": prs, "content": content}


def test_deck_slide_count_matches_the_stated_rule(real_deck):
    prs = real_deck["prs"]
    catalogue_rows = load_catalogue_rows(SKILL_DIR)
    actual = len(prs.slides._sldIdLst)
    assert actual == expected_slide_count(len(real_deck["findings"]), len(catalogue_rows))


def test_deck_has_no_shape_overflowing_the_slide_bounds(real_deck):
    """No silent overflow (CLAUDE.md §4.7 rule 3): every shape's own
    position + size stays within the slide's own dimensions. This is a
    geometry check, distinct from in-shape text overflow (covered by
    word_wrap/autofit + _ellipsize) -- it catches a mis-computed left/width
    that pushes a whole shape off the slide, the defect this test was
    written against (a _section_label call that inherited the full-width
    default instead of a narrower explicit width)."""
    prs = real_deck["prs"]
    slide_w, slide_h = prs.slide_width, prs.slide_height
    tolerance = 5000  # ~0.005in, absorbs EMU rounding only
    offenders = []
    for i, slide in enumerate(prs.slides):
        for shape in slide.shapes:
            left, top, width, height = shape.left, shape.top, shape.width, shape.height
            if None in (left, top, width, height):
                continue
            if left < -tolerance or top < -tolerance or (left + width) > slide_w + tolerance or (
                top + height
            ) > slide_h + tolerance:
                offenders.append((i, shape.name, left, top, left + width, top + height))
    assert not offenders, offenders


def test_deck_has_native_charts_not_pasted_images(real_deck):
    prs = real_deck["prs"]
    has_chart = any(shape.has_chart for slide in prs.slides for shape in slide.shapes if hasattr(shape, "has_chart"))
    assert has_chart
    has_picture = any(shape.shape_type == 13 for slide in prs.slides for shape in slide.shapes)  # MSO_SHAPE_TYPE.PICTURE
    assert not has_picture


def test_deck_footer_present_on_every_slide_with_run_id_and_timestamp(real_deck):
    run_id = real_deck["state"].run_id
    prs = real_deck["prs"]
    for i, slide in enumerate(prs.slides):
        texts = [shape.text_frame.text for shape in slide.shapes if shape.has_text_frame]
        footer_texts = [t for t in texts if t.startswith(f"run_id={run_id}")]
        assert footer_texts, f"slide {i} has no footer stamping run_id"
        assert "generated_at=" in footer_texts[0]


def test_long_observation_text_is_measured_truncated_not_silently_overflowed(real_deck):
    """Forces a finding's observation past the Top Matters slide's own
    character budget and asserts the rendered text frame is bounded and
    ellipsized -- the deck-level counterpart to test_ellipsize_* above."""
    from orchestrator.pptx_export import _OBSERVATION_MAX_CHARS, _build_top_matter
    from pptx import Presentation as _Presentation
    from pathlib import Path

    ctx, state, findings, metrics = (
        real_deck["ctx"], real_deck["state"], real_deck["findings"], real_deck["metrics"],
    )
    huge_finding = {**findings[0], "observation": "A" * 5000}
    prs = _Presentation(str(Path(DEFAULT_PPTX_TEMPLATE_PATH)))
    _build_top_matter(prs, huge_finding, metrics, state, "2026-01-01T00:00:00.000000Z", 1, 1)
    slide = prs.slides[0]
    observation_texts = [
        shape.text_frame.text for shape in slide.shapes
        if shape.has_text_frame and shape.text_frame.text.startswith("A" * 20)
    ]
    assert observation_texts, "could not find the rendered observation text frame"
    assert len(observation_texts[0]) == _OBSERVATION_MAX_CHARS
    assert observation_texts[0].endswith("…")


# ── G13-PPTX: every number on a slide equals a persisted value ─────────────


def test_g13_pptx_money_figures_match_the_specific_value_for_their_own_slide(real_deck):
    """Independent review 2026-09-24 item 8 (D6): a set-membership check --
    is this $ token equal to SOME persisted AUD value anywhere in the run --
    cannot catch a figure that is real but placed on the WRONG slide (e.g.
    finding A's exposure chip showing finding B's real, persisted amount).
    Replaces that check with PLACEMENT: the exec summary and risk/exposure
    callouts must equal the run's own headline; each Top Matters slide's
    own chip and METRICS CITED table rows must equal THAT finding's own
    values; each All Findings appendix row's Exposure cell must equal THAT
    finding's own exposure_amount (matched by rule_id, never by position).
    Slide indices are read off generate_pptx's own deterministic sequence,
    the same one test_deck_slide_count_matches_the_stated_rule asserts."""
    from orchestrator.pptx_export import _metric_value

    prs = real_deck["prs"]
    findings = real_deck["findings"]
    metrics = real_deck["metrics"]
    slides = list(prs.slides)

    headline_metric = metrics.get("run_exposure_headline")
    headline_text = _money0(headline_metric["value"] if headline_metric else None)

    def _callout_shapes(slide, text):
        # Exact-text match, never a "$"-prefix filter: the callout must
        # read "—" (NN14 / the "—" decision, §11), never a fabricated $0,
        # on a run whose headline was never computed at all.
        return [s for s in slide.shapes if s.has_text_frame and s.text_frame.text == text]

    exec_money = _callout_shapes(slides[1], headline_text)  # Executive Summary
    assert exec_money, f"no callout showing {headline_text!r} found on the Executive Summary slide"

    top = findings[:TOP_MATTERS_MAX]
    n_top = len(top)
    top_slides = slides[3: 3 + n_top]
    assert len(top_slides) == n_top
    for finding, slide in zip(top, top_slides):
        chip_texts = [s.text_frame.text for s in slide.shapes if s.has_text_frame and "Exposure:" in s.text_frame.text]
        assert chip_texts, f"{finding['finding_id']}: no Exposure chip found on its own Top Matters slide"
        exposure = finding.get("exposure_amount")
        if exposure is None:
            assert "Exposure: —" in chip_texts[0], (finding["finding_id"], chip_texts[0])
        else:
            assert f"Exposure: {_money0(exposure)}" in chip_texts[0], (finding["finding_id"], chip_texts[0])

        metrics_cited = finding.get("metrics_cited") or {}
        if metrics_cited:
            table = next((s.table for s in slide.shapes if getattr(s, "has_table", False)), None)
            assert table is not None, f"{finding['finding_id']}: METRICS CITED table missing from its own slide"
            for row in list(table.rows)[1:]:
                name = row.cells[0].text_frame.text
                value_text = row.cells[1].text_frame.text
                m = metrics_cited.get(name)
                assert m is not None, f"{finding['finding_id']}: unexpected metric row {name!r} on its own slide"
                assert value_text == _metric_value(m), (finding["finding_id"], name, value_text, m)

    risk_slide = slides[3 + n_top]  # Risk and Exposure
    risk_money = _callout_shapes(risk_slide, headline_text)
    assert risk_money, f"no callout showing {headline_text!r} found on the Risk and Exposure slide"

    n_all_slides = max(1, math.ceil(len(findings) / ALL_FINDINGS_ROWS_PER_SLIDE))
    appendix_start = 3 + n_top + 1
    checked_appendix_rows = 0
    for slide in slides[appendix_start: appendix_start + n_all_slides]:
        table = next((s.table for s in slide.shapes if getattr(s, "has_table", False)), None)
        assert table is not None, "All Findings appendix slide has no table"
        for row in list(table.rows)[1:]:
            exposure_text = row.cells[3].text_frame.text
            rule_id = row.cells[4].text_frame.text
            matches = [f for f in findings if f.get("rule_id") == rule_id]
            assert matches, f"appendix row rule_id={rule_id!r} matches no persisted finding"
            finding = matches[0]
            exposure = finding.get("exposure_amount")
            if exposure is None:
                assert exposure_text == "—", (rule_id, exposure_text)
            else:
                assert exposure_text == _money0(exposure), (rule_id, exposure_text, exposure)
            checked_appendix_rows += 1
    assert checked_appendix_rows == len(findings)


def test_g13_exec_summary_counts_equal_persisted_counts(real_deck):
    """Independent review 2026-09-24 item 8 (D9): the exec summary's own
    non-money counts -- tests assessed, findings raised, High/Medium/Low --
    equal this run's own persisted counts. Before this task the live deck
    read "assessed 21 deterministic test(s)" against a 14-test catalogue,
    because generate_pptx counted plan.yaml's own primitive INSTANCES
    (several sub-tests per catalogue test on this Skill, e.g. T3.2a_air_dom/
    _air_int/... under the one catalogue test T3.2a) rather than the
    catalogue's own tests -- the same count the Test Coverage and
    Methodology slides already state."""
    state = real_deck["state"]
    prs = real_deck["prs"]
    findings = real_deck["findings"]
    catalogue_rows = load_catalogue_rows(SKILL_DIR)

    exec_slide = list(prs.slides)[1]
    paragraph_text = next(
        s.text_frame.text for s in exec_slide.shapes
        if s.has_text_frame and s.text_frame.text.startswith("This run assessed")
    )
    n_high = sum(1 for f in findings if f.get("severity") == "High")
    n_med = sum(1 for f in findings if f.get("severity") == "Medium")
    n_low = sum(1 for f in findings if f.get("severity") == "Low")
    assert f"assessed {len(catalogue_rows)} deterministic test(s)" in paragraph_text, paragraph_text
    assert (
        f"{len(findings)} finding(s): {n_high} High, {n_med} Medium, {n_low} Low" in paragraph_text
    ), paragraph_text
    assert state.audit_period[0] in paragraph_text and state.audit_period[1] in paragraph_text


def test_g13_risk_chart_series_values_equal_severity_counts(real_deck):
    """The Risk and Exposure slide's native bar chart's own data equals the
    findings list's own severity counts, read back from the chart's cached
    values -- the chart-data leg of G13 (CLAUDE.md §5)."""
    findings = real_deck["findings"]
    prs = real_deck["prs"]
    expected = {
        "High": sum(1 for f in findings if f.get("severity") == "High"),
        "Medium": sum(1 for f in findings if f.get("severity") == "Medium"),
        "Low": sum(1 for f in findings if f.get("severity") == "Low"),
    }
    chart = None
    for slide in prs.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_chart", False):
                chart = shape.chart
                break
        if chart is not None:
            break
    assert chart is not None, "no chart found on the deck"
    categories = list(chart.plots[0].categories)
    values = list(chart.plots[0].series[0].values)
    actual = dict(zip(categories, values))
    assert actual == expected


def test_g13_exec_summary_and_risk_exposure_potential_exposure_callouts_agree(real_deck):
    """Consistency across views (this task's requirement 2, bullet 4): the
    same run_exposure_headline metric is shown twice in the deck -- once on
    the Executive Summary slide's callout box, once on the Risk and
    Exposure slide's callout box -- and both must read the same figure,
    independently computed here from the run's own persisted metrics
    (never by calling _money0/_build_exec_summary/_build_risk_and_exposure,
    the code under test)."""
    from orchestrator.pptx_export import _money0

    metrics = real_deck["metrics"]
    headline_metric = metrics.get("run_exposure_headline")
    expected_text = _money0(headline_metric["value"] if headline_metric else None)

    prs = real_deck["prs"]
    slide_titles = {}
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text in ("Executive Summary", "Risk and Exposure"):
                slide_titles[shape.text_frame.text] = slide

    callouts = {}
    for title, slide in slide_titles.items():
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.startswith("$"):
                callouts[title] = shape.text_frame.text
                break

    assert set(callouts) == {"Executive Summary", "Risk and Exposure"}
    assert callouts["Executive Summary"] == expected_text
    assert callouts["Risk and Exposure"] == expected_text


# ── code_revision_note: CLAUDE.md §11 "Paused runs across a code deploy" /
# independent review 2026-09-24 gap #11 -- both exports state "Computed
# under code revision X, exported under Y" once orchestrator.pipeline.
# run_phase has recorded a differing runs.export_code_revision for this
# run's export phase, and say nothing at all otherwise ──────────────────────


def test_code_revision_export_note_present_when_the_two_revisions_differ():
    assert (
        _code_revision_export_note("rev1", "rev2-deployed-later")
        == "Computed under code revision rev1, exported under rev2-deployed-later"
    )


@pytest.mark.parametrize(
    "computed,exported",
    [
        ("rev1", "rev1"),  # export phase re-ran on the SAME deployment
        ("rev1", None),  # export_code_revision never recorded (the common case)
        (None, "rev2"),  # a fingerprint with no code_revision at all
        (None, None),
    ],
)
def test_code_revision_export_note_absent_when_equal_or_unset(computed, exported):
    assert _code_revision_export_note(computed, exported) is None


def _run_to_export_with_export_code_revision(*, run_id: str, export_code_revision: str | None):
    """Runs the SKILL-001 planted fixture through discover..act (same node
    sequence as `real_deck` above), optionally recording an export_code_
    revision that differs from the fingerprint's own code_revision ("rev1",
    `_fingerprint`'s own fixed value) BEFORE calling `export` -- exactly the
    order orchestrator.pipeline.run_phase uses in production (it records the
    override, then runs the export-phase nodes)."""
    from tests.conftest import canonical_ts

    from orchestrator.adapters.persistence_local import LocalPersistence

    persistence = LocalPersistence(":memory:")
    persistence.migrate()
    ctx, state = _make_ctx_and_state(persistence, DATA_DIR, run_id=run_id)
    state = discover(ctx, state)
    state = execute(ctx, state)
    state = classify(ctx, state)
    state = find(ctx, state)
    state = prioritise(ctx, state)
    state = act(ctx, state)
    if export_code_revision is not None:
        persistence.record_export_code_revision(
            state.run_id, fingerprint_id=state.fingerprint_id,
            code_revision=export_code_revision, now=canonical_ts(3),
        )
    state = export(ctx, state)
    return ctx, state


def _cover_sheet_values(ctx, state) -> list:
    import io

    import openpyxl

    xlsx_bytes = ctx.export_storage.read(state.exports["xlsx"]["path"])
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    return [cell.value for row in wb["Cover"].iter_rows() for cell in row]


def test_code_revision_note_appears_on_both_exports_when_recorded_export_code_revision_differs():
    ctx, state = _run_to_export_with_export_code_revision(
        run_id="RUN-PPTX-CODEREV-DIFF", export_code_revision="rev2-deployed-later",
    )
    expected = "Computed under code revision rev1, exported under rev2-deployed-later"

    import io

    prs = Presentation(io.BytesIO(ctx.export_storage.read(state.exports["pptx"]["path"])))
    assert expected in "\n".join(_all_slide_text(prs))

    assert expected in _cover_sheet_values(ctx, state)


def test_code_revision_note_absent_from_both_exports_when_recorded_export_code_revision_equals_computed():
    ctx, state = _run_to_export_with_export_code_revision(
        run_id="RUN-PPTX-CODEREV-SAME", export_code_revision="rev1",
    )

    import io

    prs = Presentation(io.BytesIO(ctx.export_storage.read(state.exports["pptx"]["path"])))
    all_text = " ".join(_all_slide_text(prs))
    assert "Computed under code revision" not in all_text

    cover_values = _cover_sheet_values(ctx, state)
    assert not any(isinstance(v, str) and v.startswith("Computed under code revision") for v in cover_values)


def test_code_revision_note_absent_from_both_exports_when_never_recorded(real_deck):
    """The common case, on the SAME `real_deck` fixture every other G13 test
    above already exercises: no orchestrator.pipeline.run_phase override
    was ever recorded, so runs.export_code_revision is unset and neither
    export carries the note."""
    all_text = " ".join(_all_slide_text(real_deck["prs"]))
    assert "Computed under code revision" not in all_text

    cover_values = _cover_sheet_values(real_deck["ctx"], real_deck["state"])
    assert not any(isinstance(v, str) and v.startswith("Computed under code revision") for v in cover_values)


# ── zero-findings deck: an honest empty state, not a broken or fabricated
# one (CLAUDE.md §4.7 defect 5) ──────────────────────────────────────────


@pytest.fixture(scope="module")
def zero_findings_deck(tmp_path_factory):
    """A real deck rendered from a genuinely clean population -- every
    planted exception AND near-miss negative removed from plants.yaml
    (the same construction tests/test_g10_skill001_no_plants.py's G10 gate
    uses), run through the full pipeline including act/export so this file
    can assert on the RENDERED PPTX, not just on state.findings == []."""
    import yaml as _yaml

    from orchestrator.adapters.persistence_local import LocalPersistence
    from tests.fixtures.tne_planted.generate import generate

    plants_path = DATA_DIR.parent / "plants.yaml"
    doc = _yaml.safe_load(plants_path.read_text())
    no_plants_doc = {"seed": doc.get("seed"), "background": doc.get("background", {})}
    tmp_dir = tmp_path_factory.mktemp("pptx-zero-findings")
    no_plants_path = tmp_dir / "plants_no_exceptions.yaml"
    no_plants_path.write_text(_yaml.safe_dump(no_plants_doc))
    data_dir = tmp_dir / "data"
    generate(no_plants_path, data_dir)

    persistence = LocalPersistence(":memory:")
    persistence.migrate()
    ctx, state = _make_ctx_and_state(persistence, data_dir, run_id="RUN-PPTX-ZERO-FINDINGS")
    state = discover(ctx, state)
    state = execute(ctx, state)
    state = classify(ctx, state)
    state = find(ctx, state)
    state = prioritise(ctx, state)
    state = act(ctx, state)
    state = export(ctx, state)

    findings = persistence.list_findings(state.run_id)
    assert findings == [], "the no-plants fixture produced findings -- test is not exercising the zero case"
    metrics = persistence.get_run_metrics(state.run_id)

    pptx_meta = state.exports["pptx"]
    content = ctx.export_storage.read(pptx_meta["path"])
    assert hashlib.sha256(content).hexdigest() == pptx_meta["sha256"]
    import io

    prs = Presentation(io.BytesIO(content))
    return {"state": state, "findings": findings, "metrics": metrics, "prs": prs}


def test_zero_findings_deck_slide_count_matches_the_stated_rule(zero_findings_deck):
    prs = zero_findings_deck["prs"]
    catalogue_rows = load_catalogue_rows(SKILL_DIR)
    actual = len(prs.slides._sldIdLst)
    assert actual == expected_slide_count(0, len(catalogue_rows))


def test_zero_findings_deck_risk_chart_shows_all_zero_severity_counts(zero_findings_deck):
    """The one native chart in the deck must show real, honestly-computed
    zeros for a clean run -- never stale data from some other run, and
    never omitted."""
    prs = zero_findings_deck["prs"]
    chart = None
    for slide in prs.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_chart", False):
                chart = shape.chart
                break
        if chart is not None:
            break
    assert chart is not None, "no chart found on the zero-findings deck"
    categories = list(chart.plots[0].categories)
    values = list(chart.plots[0].series[0].values)
    assert dict(zip(categories, values)) == {"High": 0, "Medium": 0, "Low": 0}


def test_zero_findings_deck_says_no_control_exceptions_found_not_a_blank_section(zero_findings_deck):
    """CLAUDE.md §4.7 defect 5: a clean audit is a legitimate, important
    outcome and must say so explicitly -- never an empty 'Top Matters'
    section or a fabricated finding."""
    prs = zero_findings_deck["prs"]
    all_text = [
        shape.text_frame.text
        for slide in prs.slides
        for shape in slide.shapes
        if shape.has_text_frame
    ]
    assert any("No control exceptions found" in t for t in all_text)


def test_zero_findings_deck_exec_summary_and_risk_exposure_are_accurate(zero_findings_deck):
    """D9/D6 for the zero-findings case specifically (independent review
    2026-09-24 item 8): the audit found the zero-findings deck 'never
    rendered' at all, so neither its counts nor its callouts had ever been
    checked against real values -- this run's own catalogue count and its
    own (real, not fabricated) exposure headline, on both the Executive
    Summary and Risk and Exposure slides."""
    state = zero_findings_deck["state"]
    metrics = zero_findings_deck["metrics"]
    catalogue_rows = load_catalogue_rows(SKILL_DIR)
    prs = zero_findings_deck["prs"]
    slides = list(prs.slides)

    exec_slide = slides[1]
    paragraph_text = next(
        s.text_frame.text for s in exec_slide.shapes
        if s.has_text_frame and s.text_frame.text.startswith("This run assessed")
    )
    assert f"assessed the {state.audit_period[0]} to {state.audit_period[1]} audit" in paragraph_text
    assert "raised no findings" in paragraph_text
    assert f"assessed {len(catalogue_rows)} deterministic" not in paragraph_text  # the zero-findings p1 wording

    headline_metric = metrics.get("run_exposure_headline")
    headline_text = _money0(headline_metric["value"] if headline_metric else None)

    def _callout_shapes(slide, text):
        # Exact-text match, never a "$"-prefix filter: a clean run may
        # never compute run_exposure_headline at all, in which case the
        # callout must read "—" (NN14 / the "—" decision, §11), not a
        # fabricated $0 -- exactly what this asserts either way.
        return [s for s in slide.shapes if s.has_text_frame and s.text_frame.text == text]

    exec_money = _callout_shapes(exec_slide, headline_text)
    assert exec_money, f"no callout showing {headline_text!r} found on the Executive Summary slide"

    # slide 3 is the single "no exceptions" Top Matters slide (n_top == 0),
    # so Risk and Exposure is slide 4 -- the same "3 + n_top" arithmetic
    # test_g13_pptx_money_figures_match_the_specific_value_for_their_own_slide
    # uses on the real (non-zero) deck above.
    risk_slide = slides[4]
    risk_money = _callout_shapes(risk_slide, headline_text)
    assert risk_money, f"no callout showing {headline_text!r} found on the Risk and Exposure slide"


def test_g13_test_coverage_exception_counts_equal_test_results(real_deck):
    state = real_deck["state"]
    prs = real_deck["prs"]
    test_results_by_id = {t["test_id"]: t for t in state.test_results}
    catalogue_rows = load_catalogue_rows(SKILL_DIR)

    coverage_slide = None
    for slide in prs.slides:
        headings = [s.text_frame.text for s in slide.shapes if s.has_text_frame]
        if any("Test Coverage" == h.split(" (")[0] for h in headings):
            coverage_slide = slide
            break
    assert coverage_slide is not None

    table = next(s.table for s in coverage_slide.shapes if getattr(s, "has_table", False))
    rows = list(table.rows)[1:]  # skip header
    checked = 0
    for row in rows:
        test_id = row.cells[0].text_frame.text
        exceptions_text = row.cells[3].text_frame.text
        matching = [t for t in test_results_by_id.values() if t["test_id"] == test_id or
                    str(t["test_id"]).startswith(test_id + "_")]
        if not matching or exceptions_text == "—":
            continue
        total = sum(t.get("exception_units") or 0 for t in matching if t.get("status") == "exception")
        assert exceptions_text == f"{total:,}", (test_id, exceptions_text, total)
        checked += 1
    assert checked > 0


# ── P6 WP N11 (docs/specs/P6_narration_design.md §9): exports carry this
# run's own reviewed narration -- a narrated run (accepted-AI label present,
# rejected candidate absent from both artefacts, G13 on model paragraphs)
# and a degraded run (the exact NN13 label, fallback text byte-identical to
# the deterministic paragraphs). Uses the "mini"/"mini_candidates" Skill
# fixtures and DispatchingModelClient/RaisingModelClient
# (tests/narration_test_support.py, tests/n9_test_support.py) rather than
# the SKILL-001 planted fixture above -- narration is off in every `real_deck`/
# `zero_findings_deck` run (no `narrate` call there at all), so this is a
# second, independent pipeline run through the REAL state machine
# (`orchestrator.pipeline.run_phase`), the only way to reach a genuine
# `finalise`+`export` pass with a signed-off candidate decision. ──────────


def _read_pptx_and_xlsx(ctx, state):
    import io

    import openpyxl
    from pptx import Presentation as _Presentation

    pptx_meta = state.exports["pptx"]
    pptx_bytes = ctx.export_storage.read(pptx_meta["path"])
    assert hashlib.sha256(pptx_bytes).hexdigest() == pptx_meta["sha256"]
    prs = _Presentation(io.BytesIO(pptx_bytes))

    xlsx_meta = state.exports["xlsx"]
    xlsx_bytes = ctx.export_storage.read(xlsx_meta["path"])
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    return prs, wb


def _all_slide_text(prs) -> list[str]:
    return [
        shape.text_frame.text
        for slide in prs.slides
        for shape in slide.shapes
        if shape.has_text_frame
    ]


def _sheet_rows(ws) -> list[dict]:
    header = [c.value for c in ws[1]]
    rows = []
    for row in ws.iter_rows(min_row=2):
        values = [c.value for c in row]
        if values[0] is None or (isinstance(values[0], str) and values[0].startswith("run_id=")):
            continue
        rows.append(dict(zip(header, values)))
    return rows


@pytest.fixture(scope="module")
def narrated_deck(tmp_path_factory):
    import json as _json

    from orchestrator import service
    from orchestrator.adapters.persistence_local import LocalPersistence
    from orchestrator.narration.runner import narrative_id as _narrative_id
    from orchestrator.pipeline import run_phase
    from tests.n9_test_support import FIELDWORK_NODES_FOR, app_context_for
    from tests.narration_test_support import DispatchingModelClient, happy_responses, make_narration_harness, resp

    mini_candidates_skill_dir = Path(__file__).parent / "fixtures" / "skills" / "mini_candidates"
    marker_candidates = "finding-candidates/1"

    persistence = LocalPersistence(":memory:")
    persistence.migrate()
    tmp_path = tmp_path_factory.mktemp("pptx-narrated")

    responses = happy_responses()
    responses[marker_candidates] = resp(
        {
            "schema_version": marker_candidates,
            "candidates": [
                {
                    "title": "Missing Register Match Amount",
                    "metrics_cited": ["missing_amount"],
                    "proposed_severity": "Low",
                    "severity_reason": "This is a modest amount relative to total spend.",
                    "rationale": "No rule finding currently writes up this test's dollar impact.",
                    "observation": (
                        "The claims missing a register match total {money:missing_amount} in this run."
                    ),
                    "recommendation": "Quantify the dollar impact of unmatched claims in the review.",
                    "management_questions": ["What is the dollar impact of claims missing a register match?"],
                }
            ],
        }
    )
    client = DispatchingModelClient(responses)
    h = make_narration_harness(
        persistence, tmp_path, model_client=client, skill_dir=mini_candidates_skill_dir,
        ai_proposed_findings_enabled=True,
    )
    clock = h.ctx.clock
    fingerprint = persistence.get_fingerprint(h.state.fingerprint_id)
    state = run_phase(
        persistence, h.state.run_id, nodes_for=FIELDWORK_NODES_FOR, skill=h.ctx, clock=clock,
        current_fingerprint=fingerprint,
    )
    assert state.status == "awaiting_signoff", state.status_reason

    ctx_app = app_context_for(h, clock=clock)
    candidates = persistence.list_candidates(state.run_id)
    accepted_candidate = next(c for c in candidates if c["candidate_status"] == "candidate")

    # A second, manually-authored candidate purely to exercise "a rejected
    # candidate is absent from both exports" -- the C-2 anchor rule (§5.1)
    # allows only ONE genuine candidate from this fixture's own data
    # (missing_amount is the only metric no rule finding already cites), so
    # this one is written directly, the same pattern tests/test_finalise.py
    # already uses for a hand-built candidate row.
    rejected_id = "C-REJECTED-1"
    now = clock()
    persistence.write_candidates(
        state.run_id,
        [
            {
                "candidate_id": rejected_id, "run_id": state.run_id, "engagement_id": "ENG-DEFAULT",
                "skill_id": "SKILL-MINI-CAND", "generation": 0, "rule_id": "SKILL-MINI-CAND.ai.rejected1",
                "title": "Rejected Candidate Finding", "metrics_cited": ["hv_amount"],
                "producing_test_ids": ["T1"], "proposed_severity": "Medium",
                "severity_reason": "test scaffolding", "rationale": "test scaffolding",
                "monetary_basis": "spend", "monetary_basis_note": None, "exposure_amount": 1234.0,
                "headline_eligible": True, "headline_ineligible_reason": None, "candidate_status": "candidate",
                "call_id": "CALL-REJECTED",
            }
        ],
        now=now,
    )
    for field, text, is_list in (
        ("observation", "This candidate would report {money:hv_amount} at risk.", False),
        ("recommendation", "This text must never reach an export.", False),
        ("management_questions", ["This question must never reach an export."], True),
    ):
        persistence.upsert_narrative(
            {
                "narrative_id": _narrative_id(state.run_id, "candidate", rejected_id, field),
                "run_id": state.run_id, "engagement_id": "ENG-DEFAULT", "target_kind": "candidate",
                "target_id": rejected_id, "field": field, "version": 1, "generation": 0, "origin": "model",
                "template_text": _json.dumps(text) if is_list else text,
                "sources": [{"placeholder": "{money:hv_amount}", "source_field": "run_metrics.hv_amount", "unit": "AUD"}],
                "call_ids": ["CALL-REJECTED"], "served_model_version": "test-model-v1", "violations": None,
                "updated_by": "model", "updated_at": now,
            }
        )

    service.decide_candidate(
        ctx_app, state.run_id, accepted_candidate["candidate_id"], decision="accepted", reason=None,
        decided_severity="High", actor="alice",
    )
    service.decide_candidate(
        ctx_app, state.run_id, rejected_id, decision="rejected", reason="Not material to this audit.",
        actor="alice",
    )

    signed_off = service.sign_off(ctx_app, state.run_id, "alice")
    assert signed_off.phase == "export"

    state = run_phase(
        persistence, state.run_id, nodes_for=FIELDWORK_NODES_FOR, skill=h.ctx, clock=clock,
        current_fingerprint=fingerprint,
    )
    assert state.status == "completed", state.status_reason

    findings = persistence.list_findings(state.run_id)
    metrics = persistence.get_run_metrics(state.run_id)
    prs, wb = _read_pptx_and_xlsx(h.ctx, state)
    return {
        "ctx": h.ctx, "state": state, "findings": findings, "metrics": metrics, "prs": prs, "wb": wb,
        "accepted_candidate_id": accepted_candidate["candidate_id"], "rejected_candidate_id": rejected_id,
    }


@pytest.fixture(scope="module")
def degraded_deck(tmp_path_factory):
    from orchestrator import service
    from orchestrator.adapters.model_fake import RaisingModelClient
    from orchestrator.adapters.persistence_local import LocalPersistence
    from orchestrator.llm.errors import ModelUnavailable
    from orchestrator.pipeline import run_phase
    from tests.n9_test_support import FIELDWORK_NODES_FOR, app_context_for
    from tests.narration_test_support import MINI_SKILL_DIR, make_narration_harness

    persistence = LocalPersistence(":memory:")
    persistence.migrate()
    tmp_path = tmp_path_factory.mktemp("pptx-degraded")

    client = RaisingModelClient(ModelUnavailable("ep-sonnet", "rate limit 0", permanent=True))
    h = make_narration_harness(persistence, tmp_path, model_client=client, skill_dir=MINI_SKILL_DIR)
    clock = h.ctx.clock
    fingerprint = persistence.get_fingerprint(h.state.fingerprint_id)
    state = run_phase(
        persistence, h.state.run_id, nodes_for=FIELDWORK_NODES_FOR, skill=h.ctx, clock=clock,
        current_fingerprint=fingerprint,
    )
    assert state.status == "awaiting_signoff", state.status_reason

    ctx_app = app_context_for(h, clock=clock)
    signed_off = service.sign_off(ctx_app, state.run_id, "alice")
    assert signed_off.phase == "export"
    state = run_phase(
        persistence, state.run_id, nodes_for=FIELDWORK_NODES_FOR, skill=h.ctx, clock=clock,
        current_fingerprint=fingerprint,
    )
    assert state.status == "completed", state.status_reason

    findings = persistence.list_findings(state.run_id)
    metrics = persistence.get_run_metrics(state.run_id)
    prs, wb = _read_pptx_and_xlsx(h.ctx, state)
    return {"ctx": h.ctx, "state": state, "findings": findings, "metrics": metrics, "prs": prs, "wb": wb}


def test_narrated_deck_slide_count_is_unchanged(narrated_deck):
    prs = narrated_deck["prs"]
    findings = narrated_deck["findings"]
    catalogue_rows = load_catalogue_rows(Path(__file__).parent / "fixtures" / "skills" / "mini_candidates")
    assert len(prs.slides._sldIdLst) == expected_slide_count(len(findings), len(catalogue_rows))


def test_narrated_exec_summary_carries_the_reviewed_label(narrated_deck):
    prs = narrated_deck["prs"]
    exec_slide = list(prs.slides)[1]
    texts = [s.text_frame.text for s in exec_slide.shapes if s.has_text_frame]
    assert any(t.startswith("Model-written summary, reviewed at sign-off by alice") for t in texts), texts
    assert any("This run raised" in t and "finding(s)" in t for t in texts), texts


def test_narrated_what_we_found_shows_the_theme_and_the_accepted_ai_block(narrated_deck):
    all_text = " ".join(_all_slide_text(narrated_deck["prs"]))
    assert "Expense review gaps" in all_text
    assert "Additional matters proposed by AI review and accepted by the auditor" in all_text
    assert "Missing Register Match Amount" in all_text


def test_narrated_top_matters_carries_the_accepted_ai_label(narrated_deck):
    all_text = _all_slide_text(narrated_deck["prs"])
    assert any("AI-proposed, accepted by alice" in t for t in all_text), all_text


def test_narrated_rejected_candidate_absent_from_pptx(narrated_deck):
    all_text = " ".join(_all_slide_text(narrated_deck["prs"]))
    assert "Rejected Candidate Finding" not in all_text
    assert "This text must never reach an export" not in all_text


def test_narrated_rejected_candidate_absent_from_xlsx(narrated_deck):
    wb = narrated_deck["wb"]
    for sheet_name in wb.sheetnames:
        for row in wb[sheet_name].iter_rows():
            for cell in row:
                if isinstance(cell.value, str):
                    assert "Rejected Candidate Finding" not in cell.value
                    assert "This text must never reach an export" not in cell.value


def test_narrated_xlsx_findings_sheet_carries_origin_and_severity_columns(narrated_deck):
    wb = narrated_deck["wb"]
    rows = _sheet_rows(wb["Findings"])
    ai_rows = [r for r in rows if r["origin"] == "ai_proposed"]
    assert len(ai_rows) == 1, rows
    ai_row = ai_rows[0]
    assert ai_row["accepted_by"] == "alice"
    assert ai_row["proposed_severity"] == "Low"
    assert ai_row["decided_severity"] == "High"
    assert ai_row["observation_status"] == "ai_proposed_accepted"
    assert ai_row["recommendation_status"] == "ai_proposed_accepted"
    assert ai_row["management_questions_status"] == "ai_proposed_accepted"

    rule_rows = [r for r in rows if r["origin"] != "ai_proposed"]
    assert rule_rows
    for r in rule_rows:
        assert r["accepted_by"] in (None, "")
        # T1's happy_responses() text cites only T1's own metrics_cited
        # (hv_count/hv_amount) and validates cleanly -- T2's SHARED
        # happy_responses() text also cites {money:missing_amount}, which
        # this Skill's own T2 rule deliberately leaves out of
        # metrics_cited (the C-2 anchor this fixture exists for, see
        # tests/fixtures/skills/mini_candidates/findings.yaml) -- so that
        # placeholder is unknown to T2's own table (N-G3) and T2 falls back
        # to its reviewed template, honestly reported as such (§3.5
        # "Partial: the status is per item").
        if r["rule_id"].endswith(".T1"):
            assert r["observation_status"] == "model"
            assert r["recommendation_status"] == "model"
        elif r["rule_id"].endswith(".T2"):
            assert r["observation_status"] == "fallback_invalid"


def test_narrated_xlsx_narrative_sheet_has_status_label_and_numbers_from(narrated_deck):
    wb = narrated_deck["wb"]
    rows = _sheet_rows(wb["Narrative"])
    assert rows
    exec_rows = [r for r in rows if r["target_kind"] == "run" and r["field"] == "exec_summary"]
    assert len(exec_rows) == 1
    assert exec_rows[0]["status"] == "model"
    # NN12: run_finding_count is a computed (never a passed-through
    # run_metrics) run-level figure (orchestrator/narration/run_values.py's
    # own `_count_entry`); run_exposure_headline IS a run_metrics
    # passthrough -- the exec summary cites both.
    assert "computed:run_finding_count" in exec_rows[0]["numbers_from"]
    assert "run_metrics.run_exposure_headline" in exec_rows[0]["numbers_from"]

    ai_rows = [r for r in rows if r["label"] == "AI-proposed, accepted by alice"]
    assert ai_rows
    assert any(r["field"] == "observation" for r in ai_rows)


def test_narrated_g13_finding_prose_numbers_equal_persisted_metrics(narrated_deck):
    """G13, extended for model-written text (§9's own extension): the
    rendered `{count:hv_count}`/`{money:hv_amount}` etc. in a finding's
    observation, on BOTH exports, equal `format_metric_value` of this run's
    own persisted metric -- never a value the model invented (it cannot: the
    fixture's own happy_responses() only ever writes typed placeholders)."""
    from orchestrator.findings import format_metric_value

    metrics = narrated_deck["metrics"]
    findings = narrated_deck["findings"]
    t1 = next(f for f in findings if f.get("rule_id", "").endswith(".T1"))
    expected_count = format_metric_value(metrics["hv_count"]["value"], "count")
    expected_amount = format_metric_value(metrics["hv_amount"]["value"], "AUD")
    assert expected_count in t1["observation"]
    assert expected_amount in t1["observation"]

    all_text = " ".join(_all_slide_text(narrated_deck["prs"]))
    assert expected_count in all_text
    assert expected_amount in all_text

    wb = narrated_deck["wb"]
    findings_rows = _sheet_rows(wb["Findings"])
    t1_row = next(r for r in findings_rows if r["rule_id"].endswith(".T1"))
    assert expected_count in t1_row["observation"]
    assert expected_amount in t1_row["observation"]


def test_degraded_exec_summary_carries_the_exact_llm_unavailable_label(degraded_deck):
    from orchestrator.narration.resolve import LABEL_LLM_UNAVAILABLE, deterministic_exec_summary_paragraphs

    state = degraded_deck["state"]
    findings = degraded_deck["findings"]
    metrics = degraded_deck["metrics"]
    prs = degraded_deck["prs"]

    exec_slide = list(prs.slides)[1]
    texts = [s.text_frame.text for s in exec_slide.shapes if s.has_text_frame]
    assert any(t == LABEL_LLM_UNAVAILABLE for t in texts), texts

    expected_paragraphs = deterministic_exec_summary_paragraphs(state, findings, metrics, 0)
    joined = "\n".join(expected_paragraphs)
    paragraph_shape = next(t for t in texts if t.startswith("This run assessed"))
    assert paragraph_shape == joined


def test_degraded_what_we_found_carries_the_label_and_fixed_grouping(degraded_deck):
    from orchestrator.narration.resolve import LABEL_LLM_UNAVAILABLE

    all_text = " ".join(_all_slide_text(degraded_deck["prs"]))
    assert "fixed categorisation, not a" in all_text
    assert LABEL_LLM_UNAVAILABLE in all_text


def test_degraded_top_matters_carries_no_per_finding_label(degraded_deck):
    """§7 UI-7: the degraded label is run-level only -- never repeated on
    each Top Matters slide."""
    prs = degraded_deck["prs"]
    top_matter_slides = list(prs.slides)[3:3 + min(TOP_MATTERS_MAX, len(degraded_deck["findings"]))]
    assert top_matter_slides
    for slide in top_matter_slides:
        texts = [s.text_frame.text for s in slide.shapes if s.has_text_frame]
        assert not any("LLM unavailable" in t for t in texts), texts


def test_degraded_xlsx_findings_status_columns_are_fallback_unavailable(degraded_deck):
    wb = degraded_deck["wb"]
    rows = _sheet_rows(wb["Findings"])
    assert rows
    for r in rows:
        assert r["origin"] == "rule"
        assert r["observation_status"] == "fallback_unavailable"
        assert r["recommendation_status"] == "fallback_unavailable"
        assert r["management_questions_status"] == "fallback_unavailable"


def test_degraded_xlsx_narrative_sheet_fallback_text_equals_deterministic_text(degraded_deck):
    from orchestrator.narration.resolve import LABEL_LLM_UNAVAILABLE

    wb = degraded_deck["wb"]
    findings = degraded_deck["findings"]
    rows = _sheet_rows(wb["Narrative"])
    observation_rows = {r["target_id"]: r for r in rows if r["target_kind"] == "finding" and r["field"] == "observation"}
    assert observation_rows
    for f in findings:
        row = observation_rows[f["finding_id"]]
        assert row["status"] == "fallback_unavailable"
        assert row["label"] == LABEL_LLM_UNAVAILABLE
        assert row["text"] == f["observation"]


# ── Run inputs methodology line (independent review 2026-09-25 item 1,
#    docs/specs/P7_mapping_authoring_design.md §1.3) ────────────────────────

def _methodology_text(prs) -> list[str]:
    for slide in prs.slides:
        titles = [s.text_frame.text for s in slide.shapes if s.has_text_frame]
        if "Methodology & Limitations" in titles:
            return [s.text_frame.text for s in slide.shapes if s.has_text_frame]
    raise AssertionError("no Methodology & Limitations slide found")


def test_methodology_slide_has_no_run_inputs_line_by_default(real_deck):
    texts = _methodology_text(real_deck["prs"])
    assert not any("Project-specific inputs applied" in t for t in texts)


def test_methodology_line_text_matches_the_run_inputs_dict(real_deck):
    from orchestrator.pptx_export import _build_methodology
    import dataclasses
    from pptx import Presentation as _Presentation
    from orchestrator.skills import load_skill

    state = real_deck["state"]
    run_inputs = {
        "mappings": {"expense_report": {"Employee ID": "Emp No"}},
        "not_supplied": {"booking_detail": {"reason": "no data", "affected_tests": ["T3.1b"]}},
        "parameters": {},
    }
    state_with_inputs = dataclasses.replace(state, options={**state.options, "run_inputs": run_inputs})

    prs = _Presentation(str(DEFAULT_PPTX_TEMPLATE_PATH))
    skill = load_skill(SKILL_DIR)
    catalogue_rows = load_catalogue_rows(SKILL_DIR)
    metrics = real_deck["metrics"]
    _build_methodology(prs, state_with_inputs, metrics, skill, catalogue_rows, "2026-01-01T00:00:00Z")

    texts = [s.text_frame.text for s in prs.slides[0].shapes if s.has_text_frame]
    joined = "\n".join(texts)
    assert "Project-specific inputs applied: 1 column mapping(s), 0 parameter(s), 1 source(s) not supplied" in joined
