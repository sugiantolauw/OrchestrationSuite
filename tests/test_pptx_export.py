"""orchestrator/pptx_export.py (CLAUDE.md §4.7): the bounded slide-count
rule, measured-truncation ellipsis, no-shape-overflow geometry check, the
zero-findings path, and G13-PPTX (every number a rendered deck shows equals
a persisted value, same rounding) -- the PPTX analogue of
tests/test_p3_tne_gates.py's G13 XLSX test, against the same fast planted
fixture."""

from __future__ import annotations

import hashlib
import math
import re

import pytest
from pptx import Presentation

from orchestrator.config import DEFAULT_PPTX_TEMPLATE_PATH
from orchestrator.nodes.fieldwork import act, classify, discover, execute, export, find, prioritise
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

_MONEY_RE = re.compile(r"\$[\d,]+(?:\.\d+)?")


def _parse_money(token: str) -> float:
    return float(token.replace("$", "").replace(",", ""))


def test_g13_every_pptx_money_figure_equals_a_persisted_value(real_deck):
    """Extracts every "$..." token from every text frame and every table
    cell across the whole deck (CLAUDE.md §5 G13) and asserts each one, once
    parsed, equals a persisted figure at the SAME rounding it was displayed
    with (whole-dollar tokens against the 0dp KPI rounding, tokens with
    cents against orchestrator.findings.format_metric_value's 2dp rounding)
    -- never a number the export step invented. Scoped to money figures
    (the audit-evidence numbers CLAUDE.md §4.7/§5 are about), not every
    incidental digit on a slide (a test_id like "T3.1a", a date, a run_id) --
    the same scoping G13's own XLSX sibling test uses (specific sheets/
    columns, not a blind character scan)."""
    prs = real_deck["prs"]
    findings = real_deck["findings"]
    metrics = real_deck["metrics"]

    aud_metric_values = [m.get("value") for m in metrics.values() if m.get("unit") == "AUD"]
    finding_exposures = [f.get("exposure_amount") for f in findings]
    allowed_0dp = {round(v) for v in [*aud_metric_values, *finding_exposures] if isinstance(v, (int, float))}
    allowed_2dp = {round(v, 2) for v in [*aud_metric_values, *finding_exposures] if isinstance(v, (int, float))}

    def _cell_texts(table):
        for row in table.rows:
            for cell in row.cells:
                yield cell.text_frame.text

    checked = 0
    offenders = []
    for slide in prs.slides:
        for shape in slide.shapes:
            texts = []
            if shape.has_text_frame:
                texts.append(shape.text_frame.text)
            if getattr(shape, "has_table", False):
                texts.extend(_cell_texts(shape.table))
            for text in texts:
                for token in _MONEY_RE.findall(text):
                    value = _parse_money(token)
                    checked += 1
                    has_cents = "." in token
                    ok = (value in allowed_2dp) if has_cents else (round(value) in allowed_0dp)
                    if not ok:
                        offenders.append((token, value, text[:80]))
    assert checked > 0, "no money figures found on the deck -- test is not exercising anything"
    assert not offenders, offenders


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
