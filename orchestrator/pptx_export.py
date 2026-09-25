"""The PPTX audit pack (CLAUDE.md §4.7). Built from a completed run's own
persisted outputs ONLY -- `state`, `findings` (persistence.list_findings),
`metrics` (persistence.get_run_metrics) and `catalogue_rows` (this Skill's
own catalogue.yaml, static per Skill, never per-run data) -- never module
globals, never a recomputation of a number `execute`/`find`/`prioritise`
already fixed. Every number placed on a slide traces to one of those three
inputs; G13 (tests/test_p3_tne_gates.py) asserts the numbers a rendered deck
actually shows equal them.

Rebuilds reference_app/src/pptx_export.py's six defects (CLAUDE.md §4.7):

  1. Bounded, deterministic slide count -- `expected_slide_count()` below is
     the single source of truth `generate_pptx()` asserts itself against.
  2. Every text frame gets `word_wrap` + `MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE` AS
     WELL AS a measured character-budget truncation with an explicit
     ellipsis (`_ellipsize`) for the two fields whose length this codebase
     does not otherwise bound (a finding's observation/recommendation
     prose) -- autofit alone is a PowerPoint-render-time behaviour this
     process cannot verify before shipping the file; the character budget
     is a hard, testable guarantee.
  3. The executive summary is written prose. Since P6 WP N11
     (docs/specs/P6_narration_design.md §9) it is this run's own reviewed
     `exec_summary` narrative when narration produced one, rendered through
     `orchestrator.narration.resolve.effective_prose` -- falling back to
     `resolve.deterministic_exec_summary_paragraphs` (the SAME three
     paragraphs this module used to build inline) with an exact NN13 label
     when it did not. Not bare counts either way.
  4. The headline figure is `run_exposure_headline` (CLAUDE.md independent
     review 2026-09-24 item 1's "amount at risk"), read from `metrics`,
     never recomputed by summing findings' own (deliberately overlapping)
     `exposure_amount` figures.
  5. Zero findings gets its own slide ("No control exceptions found"),
     never an empty Top Matters section.
  6. The template (templates/report_template.pptx) already carries zero
     content slides -- scripts/strip_pptx_template.py did that stripping
     once, offline, using python-pptx internals the way defect 6 describes;
     this module only ever calls `prs.slides.add_slide(layout)`, the public
     API, at runtime.
"""

from __future__ import annotations

import io
import math
from pathlib import Path

import yaml
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.util import Inches, Pt

from orchestrator.catalogue_counts import catalogue_id_for, combined_status, results_for
from orchestrator.findings import format_metric_value
from orchestrator.narration import resolve as narration_resolve
from orchestrator.signoff_policy import SELF_APPROVED_LABEL
from orchestrator.state import RunState

# ── brand-neutral palette -- the same hex values app/src/charts.py already
# uses for SEVERITY_COLOR, so a PPTX finding reads the same colour as the
# live /workspace/tne chart it was exported from, not a second unrelated
# palette (CLAUDE.md §4.7 "dataviz" skill: a status palette is reserved and
# consistent, never re-invented per surface). ──────────────────────────────
TEAL = RGBColor(0x1E, 0x27, 0x61)
DARK_TEAL = RGBColor(0x00, 0x32, 0x3A)
BODY_TEXT = RGBColor(0x1A, 0x1A, 0x1A)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
LIGHT_BG = RGBColor(0xF5, 0xF6, 0xF8)
GREY = RGBColor(0x6B, 0x72, 0x83)
LIGHT_GREY = RGBColor(0xE4, 0xE7, 0xEE)
RED = RGBColor(0xB8, 0x50, 0x42)
AMBER = RGBColor(0xE0, 0x95, 0x2A)
GREEN = RGBColor(0x2C, 0x7A, 0x4B)

SEVERITY_COLOR = {"High": RED, "Medium": AMBER, "Low": GREEN}
STATUS_COLOR = {"exception": RED, "pass": GREEN, "not_testable": GREY}
STATUS_LABEL = {"exception": "Exception", "pass": "Pass", "not_testable": "Not testable"}

FONT_HEAVY = "MarkOT-Heavy"
FONT_BODY = "MarkOT"

# Layout indices in templates/report_template.pptx (verified against the
# source deck by scripts/strip_pptx_template.py's own assertions -- these
# never change since stripping content slides never touches slide_layouts).
LY_COVER = 3
LY_DIVIDER = 6
LY_TITLE_ONLY = 35
LY_END = 57

SLIDE_W = 13.33
SLIDE_H = 7.50
CONTENT_LEFT = 0.55
CONTENT_RIGHT = 12.78
CONTENT_W = CONTENT_RIGHT - CONTENT_LEFT

# ── the bounded, deterministic slide-count rule (CLAUDE.md §4.7 defect 1) ──
TOP_MATTERS_MAX = 5
ALL_FINDINGS_ROWS_PER_SLIDE = 16
COVERAGE_ROWS_PER_SLIDE = 20


def expected_slide_count(n_findings: int, n_catalogue_tests: int) -> int:
    """The single source of truth for how many slides a deck of this shape
    MUST have -- `generate_pptx()` asserts its own output against this at
    the end of the function, so a future slide-builder change that silently
    adds/drops a slide fails loudly (an assertion error) rather than
    shipping an unbounded, undocumented deck.

    Cover(1) + Executive summary(1) + What we found(1)
      + (1 "no exceptions" slide if n_findings == 0, else min(5, n_findings)
         Top Matters slides)
      + Risk and exposure(1)
      + ceil(n_findings / ALL_FINDINGS_ROWS_PER_SLIDE), at least 1, for the
        All Findings appendix table (a zero-findings run still gets one
        slide stating that)
      + ceil(n_catalogue_tests / COVERAGE_ROWS_PER_SLIDE), at least 1, for
        Test Coverage
      + Methodology & limitations(1)
      + End(1)
    """
    top_matters = 1 if n_findings == 0 else min(TOP_MATTERS_MAX, n_findings)
    all_findings_slides = max(1, math.ceil(n_findings / ALL_FINDINGS_ROWS_PER_SLIDE))
    coverage_slides = max(1, math.ceil(n_catalogue_tests / COVERAGE_ROWS_PER_SLIDE))
    return 1 + 1 + 1 + top_matters + 1 + all_findings_slides + coverage_slides + 1 + 1


# ── formatting -- every one of these mirrors an existing, already-shipped
# formatter (app/src/workspace_tne.py / orchestrator/findings.py) so a
# number reads exactly the same on a slide as it does in the UI or in the
# finding prose it may be quoting verbatim (CLAUDE.md "same rounding"). ────


def _money0(value) -> str:
    """Whole-dollar KPI-style rounding -- mirrors app/src/workspace_tne.py's
    _potential_exposure_value/_fmt_currency (the "Potential exposure" KPI
    tile). Never a fabricated $0 for a missing figure (CLAUDE.md NN14)."""
    return f"${value:,.0f}" if isinstance(value, (int, float)) else "—"


def _count0(value) -> str:
    return f"{int(round(value)):,}" if isinstance(value, (int, float)) else "—"


def _metric_value(m: dict) -> str:
    """Mirrors orchestrator.findings.format_metric_value -- the same
    formatter a finding's own observation/recommendation prose is already
    rendered with, so a metrics-cited table reads identically to the prose
    quoting the same metric."""
    return format_metric_value(m.get("value"), m.get("unit"))


def _source_ref_text(source_ref: dict | None) -> str:
    sources = (source_ref or {}).get("sources") or []
    text = ", ".join(f"{s.get('name')}@{s.get('version')}" for s in sources if s.get("name"))
    return text or "—"


def _ellipsize(text: str, max_chars: int) -> str:
    """Measured truncation with an explicit ellipsis (CLAUDE.md §4.7 defect
    2) for the two fields a Skill author can make arbitrarily long -- a
    finding's `observation`/`recommendation` prose (rule.observation in
    findings.yaml, filled with real numbers at run time) and a
    not-fully-bounded methodology bullet. `max_chars` is chosen per call
    site as a conservative character budget for that text box's own
    width/height/font size (documented at each call site), not a generic
    formula -- this function's only job is to apply that budget exactly,
    the same way on every call, so a test can assert it."""
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"


# ── low-level shape helpers ─────────────────────────────────────────────────


def _font(run, size=11, bold=False, color=BODY_TEXT, name=None):
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = color
    run.font.name = name or FONT_BODY


def _textbox(slide, left, top, width, height, text, *, size=11, bold=False, color=BODY_TEXT,
             alignment=PP_ALIGN.LEFT, name=None):
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    tf = box.text_frame
    tf.word_wrap = True
    tf.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
    p = tf.paragraphs[0]
    p.alignment = alignment
    run = p.add_run()
    run.text = text
    _font(run, size, bold, color, name)
    return tf


def _add_para(tf, text, *, size=11, bold=False, color=BODY_TEXT, space_before=0, name=None):
    p = tf.add_paragraph()
    p.space_before = Pt(space_before)
    run = p.add_run()
    run.text = text
    _font(run, size, bold, color, name)
    return p


def _section_label(slide, left, top, text, width=CONTENT_W):
    _textbox(slide, left, top, width, 0.25, text, size=9, bold=True, color=TEAL, name=FONT_HEAVY)


def _set_cell(cell, text, *, size=9, bold=False, color=BODY_TEXT, alignment=PP_ALIGN.LEFT):
    cell.text = ""
    tf = cell.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.alignment = alignment
    run = p.add_run()
    run.text = str(text)
    _font(run, size, bold, color)
    cell.vertical_anchor = MSO_ANCHOR.MIDDLE


def _footer(slide, *, run_id: str, generated_at: str, extra: str | None = None, color=GREY):
    """Every slide's own footer (CLAUDE.md §4.7 rule 3 / §9A.2): run_id +
    generation timestamp, so an exported deck is traceable to the run that
    produced it wherever it ends up. `extra` carries the self-approval /
    approver note on the cover slide only. `color` defaults to GREY for the
    white-background "Title Only"/"Blank" layouts every content slide uses;
    the Cover/End layouts are solid teal, so their own calls pass WHITE --
    GREY-on-teal would be unreadable."""
    text = f"run_id={run_id} | generated_at={generated_at}"
    if extra:
        text += f" | {extra}"
    _textbox(slide, CONTENT_LEFT, SLIDE_H - 0.32, CONTENT_W, 0.28, text, size=7.5, color=color)


#  A Top Matters slide's own title concatenates rank/total, a test_id and a
#  finding's title (findings.yaml, up to ~70 chars in this Skill's own
#  catalogue) -- the one title this module builds from content whose length
#  it does not otherwise control. Budgeted generously for the title
#  placeholder's own width at the template's title font size.
_TITLE_MAX_CHARS = 90


def _new_slide(prs, layout_idx: int, title: str | None = None):
    slide = prs.slides.add_slide(prs.slide_layouts[layout_idx])
    if title is not None and slide.placeholders:
        try:
            title_ph = slide.placeholders[0]
        except KeyError:
            title_ph = None
        if title_ph is not None:
            # Rule 3 applies to every text frame, including template
            # placeholders (not just this module's own _textbox calls) --
            # word_wrap + autofit as the PowerPoint-render-time fallback,
            # plus a measured truncation as the hard, testable guarantee.
            title_ph.text = _ellipsize(title, _TITLE_MAX_CHARS)
            title_ph.text_frame.word_wrap = True
            title_ph.text_frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
    return slide


# ── catalogue / test-category helpers (CLAUDE.md §4.7 "What we found":
# deterministic grouping by test category/control area, not LLM themes) ────


def load_catalogue_rows(skill_dir: Path) -> list[dict]:
    catalogue_path = Path(skill_dir) / "catalogue.yaml"
    if not catalogue_path.is_file():
        return []
    data = yaml.safe_load(catalogue_path.read_text()) or {}
    return list(data.get("tests") or [])


# catalogue-grain grouping (exact-id-or-"<id>_"-prefix matching, combined
# status) now lives in orchestrator.catalogue_counts, shared with
# orchestrator/narration/run_values.py (independent review 2026-09-24 gap
# #5). `catalogue_id_for`/`results_for`/`combined_status` imported above.


def _reconciliation_ok(rec: dict) -> bool:
    variance = rec.get("variance")
    amount_variance = rec.get("amount_variance")
    date_ok = rec.get("min_date_match") is not False and rec.get("max_date_match") is not False
    return variance in (0, None) and amount_variance in (0, 0.0, None) and date_ok


# ── slide builders ───────────────────────────────────────────────────────────


def _build_cover(
    prs, state: RunState, skill_manifest: dict, data_mode: str, now: str,
    code_revision_note: str | None = None,
):
    slide = _new_slide(prs, LY_COVER)
    title_ph = slide.placeholders[0]
    title_ph.text = skill_manifest.get("domain") or skill_manifest.get("name", "Audit Report")
    for run in title_ph.text_frame.paragraphs[0].runs:
        _font(run, 36, True, WHITE, FONT_HEAVY)

    sub_ph = slide.placeholders[1]
    sub_ph.text = "Audit Analytics Report"
    for run in sub_ph.text_frame.paragraphs[0].runs:
        _font(run, 18, False, WHITE, FONT_BODY)

    detail_ph = slide.placeholders[16]
    period = f"{state.audit_period[0]} to {state.audit_period[1]}"
    lines = [
        f"{skill_manifest.get('id', state.skill_id)} {skill_manifest.get('name', '')} "
        f"v{skill_manifest.get('version', state.skill_version)}",
        f"Audit period: {period}  ·  Data mode: {data_mode}",
        f"Generated {now}",
    ]
    signoff = state.signoff or {}
    if signoff.get("approver"):
        # P7 review workflow (docs/specs/P7_mapping_authoring_design.md §3.7):
        # "Prepared by P · Reviewed by R · Signed off by A on T" once a
        # signoff carries prepared_by (the P7-gated path); a legacy signoff
        # (no prepared_by) keeps its original one-part line unchanged.
        if signoff.get("prepared_by") is not None:
            line = (
                f"Prepared by {signoff.get('prepared_by')} · Reviewed by {signoff.get('reviewed_by')} · "
                f"Signed off by {signoff.get('approver')} on {signoff.get('timestamp', '—')}"
            )
        else:
            line = f"Signed off by {signoff.get('approver')}"
        if signoff.get("self_approved"):
            line += f" — {SELF_APPROVED_LABEL}"
        lines.append(line)
    # CLAUDE.md §11 "Paused runs across a code deploy" / independent review
    # 2026-09-24 gap #11: absent for the common case (exported under the
    # SAME code revision `execute` computed this run's numbers under, or no
    # export_code_revision recorded at all) -- present, never fabricated,
    # only once orchestrator.pipeline.run_phase has actually recorded a
    # differing export_code_revision for this run.
    if code_revision_note:
        lines.append(code_revision_note)
    detail_ph.text = lines[0]
    tf = detail_ph.text_frame
    tf.word_wrap = True
    for run in tf.paragraphs[0].runs:
        _font(run, 12, False, WHITE, FONT_BODY)
    for line in lines[1:]:
        p = tf.add_paragraph()
        run = p.add_run()
        run.text = line
        _font(run, 12, False, WHITE, FONT_BODY)
    _footer(slide, run_id=state.run_id, generated_at=now, color=WHITE)


def _build_exec_summary(
    prs, state: RunState, findings: list[dict], metrics: dict, n_tests: int, now: str, narration: dict,
):
    slide = _new_slide(prs, LY_TITLE_ONLY, "Executive Summary")

    # P6 WP N11 (docs/specs/P6_narration_design.md §9): this run's own
    # reviewed `exec_summary` narrative, already resolved by
    # `orchestrator.nodes.fieldwork._build_export_narration` -- `None` means
    # no model text was accepted (narration off, unavailable or invalid),
    # in which case this falls back to the SAME deterministic three
    # paragraphs the module used to build inline, now the one shared
    # implementation (`resolve.deterministic_exec_summary_paragraphs`) so
    # this copy and `resolve.py`'s own copy can never drift apart.
    paragraphs = narration.get("exec_summary_paragraphs")
    label = narration.get("exec_summary_label") or narration_resolve.LABEL_LLM_UNAVAILABLE
    if not paragraphs:
        paragraphs = narration_resolve.deterministic_exec_summary_paragraphs(state, findings, metrics, n_tests)

    _textbox(slide, CONTENT_LEFT, 0.85, CONTENT_W, 0.4, label, size=10, bold=True, color=GREY)

    headline_metric = metrics.get("run_exposure_headline")
    headline_value = headline_metric["value"] if headline_metric else None

    tf = _textbox(slide, CONTENT_LEFT, 1.35, CONTENT_W * 0.62, 3.6, paragraphs[0], size=13, color=BODY_TEXT)
    for p in paragraphs[1:]:
        _add_para(tf, p, size=13, color=BODY_TEXT, space_before=12)

    callout_left = CONTENT_LEFT + CONTENT_W * 0.66
    callout_w = CONTENT_W * 0.34
    box = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(callout_left), Inches(1.35),
                                  Inches(callout_w), Inches(1.5))
    box.fill.solid()
    box.fill.fore_color.rgb = LIGHT_BG
    box.line.color.rgb = LIGHT_GREY
    _textbox(slide, callout_left + 0.2, 1.55, callout_w - 0.4, 0.35, "POTENTIAL EXPOSURE",
             size=10, bold=True, color=GREY)
    _textbox(slide, callout_left + 0.2, 1.9, callout_w - 0.4, 0.7, _money0(headline_value),
             size=30, bold=True, color=TEAL, name=FONT_HEAVY)
    _footer(slide, run_id=state.run_id, generated_at=now)


def _build_what_we_found(
    prs, findings: list[dict], catalogue_rows: list[dict], state: RunState, now: str, narration: dict,
):
    slide = _new_slide(prs, LY_TITLE_ONLY, "What We Found")

    # P6 WP N11 (§9): this run's own confirmed synthesis themes (already
    # resolved -- rendered title/summary/root-cause, member findings --
    # replace the fixed catalogue-category grouping below when there are
    # any; a `themes_label` is set ONLY on the fallback path (§9's own
    # table: "today's catalogue grouping, WITH THE LABEL"), never when real
    # themes are shown.
    themes = narration.get("themes") or []
    accepted_ai = narration.get("accepted_ai_findings") or []
    themes_label = narration.get("themes_label")

    if themes:
        subtitle = "Findings grouped into themes identified from this run's own results."
    else:
        subtitle = (
            "Findings grouped by test category / control area — a fixed categorisation, not a "
            "generated theme."
        )
    if themes_label:
        subtitle = f"{subtitle}  ·  {themes_label}"
    _textbox(slide, CONTENT_LEFT, 0.85, CONTENT_W, 0.4, subtitle, size=10, color=GREY)

    tf = None

    def _member_line(m: dict) -> str:
        return f"  •  [{m.get('severity', '—')}] {m.get('title', '—')}"

    if themes:
        for theme in themes:
            header = theme["title"]
            if tf is None:
                tf = _textbox(slide, CONTENT_LEFT, 1.3, CONTENT_W, 4.5, header, size=13, bold=True, color=TEAL)
            else:
                _add_para(tf, header, size=13, bold=True, color=TEAL, space_before=12)
            _add_para(tf, theme["summary"], size=11, color=BODY_TEXT, space_before=3)
            root_cause = theme.get("root_cause")
            if root_cause:
                _add_para(
                    tf, f"Root-cause hypothesis (for discussion): {root_cause}", size=9.5, color=GREY,
                    space_before=3,
                )
            for m in theme["members"]:
                _add_para(tf, _member_line(m), size=10.5, color=BODY_TEXT, space_before=2)
    elif findings:
        catalogue_ids = {t["test_id"] for t in catalogue_rows if t.get("test_id")}
        category_by_id = {t["test_id"]: t.get("category", "Uncategorised") for t in catalogue_rows}
        groups: dict[str, list[dict]] = {}
        for f in findings:
            if f.get("origin") == "ai_proposed":
                continue  # shown in the fixed AI-proposed block below, never inside a rule-test category
            cat_id = catalogue_id_for(f.get("test_id") or "", catalogue_ids)
            category = category_by_id.get(cat_id, "Uncategorised")
            groups.setdefault(category, []).append(f)
        for category in sorted(groups):
            items = groups[category]
            n_high = sum(1 for f in items if f.get("severity") == "High")
            header = f"{category} — {len(items)} finding(s), {n_high} High"
            if tf is None:
                tf = _textbox(slide, CONTENT_LEFT, 1.3, CONTENT_W, 4.5, header, size=13, bold=True, color=TEAL)
            else:
                _add_para(tf, header, size=13, bold=True, color=TEAL, space_before=12)
            for f in items:
                _add_para(tf, _member_line(f), size=11.5, color=BODY_TEXT, space_before=3)

    if accepted_ai:
        header = "Additional matters proposed by AI review and accepted by the auditor"
        if tf is None:
            tf = _textbox(slide, CONTENT_LEFT, 1.3, CONTENT_W, 4.5, header, size=13, bold=True, color=TEAL)
        else:
            _add_para(tf, header, size=13, bold=True, color=TEAL, space_before=12)
        for f in accepted_ai:
            _add_para(tf, _member_line(f), size=11.5, color=BODY_TEXT, space_before=3)

    if tf is None:
        tf = _textbox(slide, CONTENT_LEFT, 1.3, CONTENT_W, 4.5, "No control exceptions found in this run.",
                       size=14, color=BODY_TEXT)
    _footer(slide, run_id=state.run_id, generated_at=now)


def _build_no_findings(prs, state: RunState, now: str):
    slide = _new_slide(prs, LY_TITLE_ONLY, "Top Matters")
    _textbox(slide, CONTENT_LEFT, 2.6, CONTENT_W, 1.5, "No control exceptions found",
             size=26, bold=True, color=GREEN, name=FONT_HEAVY, alignment=PP_ALIGN.CENTER)
    _textbox(slide, CONTENT_LEFT, 3.5, CONTENT_W, 0.8,
             "Every deterministic test in this run's plan either passed or was not testable. "
             "See Test coverage and Methodology & limitations for detail.",
             size=13, color=GREY, alignment=PP_ALIGN.CENTER)
    _footer(slide, run_id=state.run_id, generated_at=now)


# CLAUDE.md §4.7 defect 2: a finding's observation/recommendation is
# free-form template text (findings.yaml) filled with real, unbounded
# numbers at run time -- the ~4.6in x 1.7in observation box at 11pt fits
# roughly 500 characters comfortably in the template's MarkOT font; recommendation
# gets a shorter box, budgeted the same way.
_OBSERVATION_MAX_CHARS = 480
_RECOMMENDATION_MAX_CHARS = 320


def _build_top_matter(prs, finding: dict, metrics: dict, state: RunState, now: str, rank: int, total: int):
    slide = _new_slide(prs, LY_TITLE_ONLY, f"{rank}/{total}  {finding.get('test_id', '')}: {finding.get('title', '')}")
    severity = finding.get("severity", "—")
    badge = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(CONTENT_LEFT), Inches(1.1),
                                    Inches(1.1), Inches(0.35))
    badge.fill.solid()
    badge.fill.fore_color.rgb = SEVERITY_COLOR.get(severity, GREY)
    badge.line.fill.background()
    badge.text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER
    run = badge.text_frame.paragraphs[0].add_run()
    run.text = severity
    _font(run, 10, True, WHITE)

    chip_parts = []
    exposure = finding.get("exposure_amount")
    if exposure is not None:
        chip_parts.append(f"Exposure: {_money0(exposure)}")
    else:
        chip_parts.append(f"Exposure: — ({finding.get('exposure_basis', 'non-monetary finding')})")
    if finding.get("analyst_set_severity"):
        chip_parts.append("analyst-set threshold, pending policy confirmation")
    # P6 WP N11 (§9): "An accepted candidate carries its label line" -- the
    # ONLY per-finding label a Top Matters slide shows (never the run-level
    # degraded label, §7 UI-7); `prose_label` is unset for every rule
    # finding regardless of narration status.
    if finding.get("prose_label"):
        chip_parts.append(finding["prose_label"])
    _textbox(slide, CONTENT_LEFT + 1.3, 1.12, 7.5, 0.3, "  ·  ".join(chip_parts), size=10, color=GREY)

    _section_label(slide, CONTENT_LEFT, 1.65, "OBSERVATION")
    _textbox(slide, CONTENT_LEFT, 1.9, CONTENT_W * 0.64, 1.55,
             _ellipsize(finding.get("observation") or "—", _OBSERVATION_MAX_CHARS), size=11, color=BODY_TEXT)

    _section_label(slide, CONTENT_LEFT, 3.55, "RECOMMENDATION")
    _textbox(slide, CONTENT_LEFT, 3.8, CONTENT_W * 0.64, 1.05,
             _ellipsize(finding.get("recommendation") or "—", _RECOMMENDATION_MAX_CHARS), size=11, color=BODY_TEXT)

    questions = finding.get("management_questions") or []
    if questions:
        _section_label(slide, CONTENT_LEFT, 5.0, "MANAGEMENT QUESTION")
        _textbox(slide, CONTENT_LEFT, 5.25, CONTENT_W * 0.64, 1.1,
                 _ellipsize(questions[0], 220), size=10.5, color=BODY_TEXT)

    metrics_cited = finding.get("metrics_cited") or {}
    if metrics_cited:
        m_left = CONTENT_LEFT + CONTENT_W * 0.67
        m_w = CONTENT_W * 0.33
        _section_label(slide, m_left, 1.65, "METRICS CITED", width=m_w)
        n_m = min(len(metrics_cited), 8)
        tbl = slide.shapes.add_table(n_m + 1, 3, Inches(m_left), Inches(1.95),
                                      Inches(m_w), Inches(0.3 * (n_m + 1))).table
        tbl.columns[0].width = Inches(m_w * 0.36)
        tbl.columns[1].width = Inches(m_w * 0.24)
        tbl.columns[2].width = Inches(m_w * 0.40)
        for j, h in enumerate(["Metric", "Value", "Source"]):
            cell = tbl.cell(0, j)
            _set_cell(cell, h, size=8, bold=True, color=WHITE)
            cell.fill.solid()
            cell.fill.fore_color.rgb = TEAL
        for i, (name, m) in enumerate(list(metrics_cited.items())[:8]):
            _set_cell(tbl.cell(i + 1, 0), name, size=7, color=GREY)
            _set_cell(tbl.cell(i + 1, 1), _metric_value(m), size=8, bold=True, color=BODY_TEXT,
                      alignment=PP_ALIGN.RIGHT)
            _set_cell(tbl.cell(i + 1, 2), _source_ref_text(m.get("source_ref")), size=7, color=GREY)

    _footer(slide, run_id=state.run_id, generated_at=now)


_RISK_CHART_CAPTION_MAX_CHARS = 200


def _build_risk_and_exposure(
    prs, findings: list[dict], metrics: dict, state: RunState, now: str, narration: dict,
):
    slide = _new_slide(prs, LY_TITLE_ONLY, "Risk and Exposure")
    n_high = sum(1 for f in findings if f.get("severity") == "High")
    n_med = sum(1 for f in findings if f.get("severity") == "Medium")
    n_low = sum(1 for f in findings if f.get("severity") == "Low")

    chart_data = CategoryChartData()
    chart_data.categories = ["High", "Medium", "Low"]
    chart_data.add_series("Findings", (n_high, n_med, n_low))
    chart_frame = slide.shapes.add_chart(
        XL_CHART_TYPE.BAR_CLUSTERED, Inches(CONTENT_LEFT), Inches(1.2), Inches(6.0), Inches(5.2), chart_data,
    )
    chart = chart_frame.chart
    chart.has_legend = False
    plot = chart.plots[0]
    plot.has_data_labels = True
    plot.data_labels.font.size = Pt(11)
    plot.data_labels.font.bold = True
    plot.data_labels.number_format = "0"
    plot.data_labels.position = XL_LABEL_POSITION.OUTSIDE_END
    series = plot.series[0]
    for i, color in enumerate((RED, AMBER, GREEN)):
        pt = series.points[i]
        pt.format.fill.solid()
        pt.format.fill.fore_color.rgb = color
    chart.category_axis.tick_labels.font.size = Pt(11)
    chart.value_axis.has_major_gridlines = False

    # P6 WP N11 (§9): one narrated caption under the chart, ≤200 characters
    # (`_ellipsize` is the same measured-truncation guarantee every other
    # unbounded field in this module uses, defect 2) -- no caption at all on
    # the fallback path (§9's own table: "Fallback: no caption"), never a
    # placeholder line.
    caption = narration.get("risk_chart_caption")
    if caption:
        _textbox(slide, CONTENT_LEFT, 6.55, 6.0, 0.5, _ellipsize(caption, _RISK_CHART_CAPTION_MAX_CHARS),
                 size=9.5, color=GREY)

    headline_metric = metrics.get("run_exposure_headline")
    headline_value = headline_metric["value"] if headline_metric else None
    approved_not_spent_metric = metrics.get("run_approved_not_spent_total")
    approved_not_spent_value = approved_not_spent_metric["value"] if approved_not_spent_metric else None

    note_left = CONTENT_LEFT + 6.3
    note_w = CONTENT_W - 6.3
    _section_label(slide, note_left, 1.2, "POTENTIAL EXPOSURE", width=note_w)
    _textbox(slide, note_left, 1.45, note_w, 0.6, _money0(headline_value), size=26, bold=True,
             color=TEAL, name=FONT_HEAVY)
    tf = _textbox(
        slide, note_left, 2.15, note_w, 3.9,
        "The amount at risk: every distinct flagged transaction line counts exactly once, at the "
        "largest amount any finding attributes to it.", size=10.5, color=BODY_TEXT,
    )
    _add_para(
        tf,
        "Findings' own exposure_amount figures (shown on each Top Matters slide) legitimately "
        "overlap with each other and with this headline — the same line can be cited by more than "
        "one finding — and must never be summed.",
        size=10.5, color=GREY, space_before=10,
    )
    if approved_not_spent_value:
        _add_para(
            tf, f"Approved but never spent (not flagged spend, excluded above): "
                f"{_money0(approved_not_spent_value)}.",
            size=10.5, color=GREY, space_before=10,
        )
    _footer(slide, run_id=state.run_id, generated_at=now)


def _build_all_findings_appendix(prs, findings: list[dict], state: RunState, now: str):
    if not findings:
        slide = _new_slide(prs, LY_TITLE_ONLY, "All Findings")
        _textbox(slide, CONTENT_LEFT, 1.3, CONTENT_W, 1.0, "No findings this run.", size=14, color=BODY_TEXT)
        _footer(slide, run_id=state.run_id, generated_at=now)
        return

    n_slides = max(1, math.ceil(len(findings) / ALL_FINDINGS_ROWS_PER_SLIDE))
    for s in range(n_slides):
        chunk = findings[s * ALL_FINDINGS_ROWS_PER_SLIDE:(s + 1) * ALL_FINDINGS_ROWS_PER_SLIDE]
        title = "All Findings" if n_slides == 1 else f"All Findings ({s + 1}/{n_slides})"
        slide = _new_slide(prs, LY_TITLE_ONLY, title)
        n_rows = len(chunk) + 1
        tbl = slide.shapes.add_table(n_rows, 5, Inches(CONTENT_LEFT), Inches(1.15),
                                      Inches(CONTENT_W), Inches(min(0.32 * n_rows, 5.7))).table
        tbl.columns[0].width = Inches(1.0)
        tbl.columns[1].width = Inches(5.0)
        tbl.columns[2].width = Inches(1.3)
        tbl.columns[3].width = Inches(1.6)
        tbl.columns[4].width = Inches(3.88)
        headers = ["Test", "Title", "Severity", "Exposure", "Rule ID"]
        for j, h in enumerate(headers):
            cell = tbl.cell(0, j)
            _set_cell(cell, h, size=9, bold=True, color=WHITE)
            cell.fill.solid()
            cell.fill.fore_color.rgb = TEAL
        for i, f in enumerate(chunk):
            severity = f.get("severity", "—")
            _set_cell(tbl.cell(i + 1, 0), f.get("test_id", "—"), size=8, color=TEAL, bold=True)
            _set_cell(tbl.cell(i + 1, 1), _ellipsize(f.get("title", "—"), 70), size=8, color=BODY_TEXT)
            _set_cell(tbl.cell(i + 1, 2), severity, size=8, bold=True, color=SEVERITY_COLOR.get(severity, GREY))
            exposure = f.get("exposure_amount")
            _set_cell(tbl.cell(i + 1, 3), _money0(exposure) if exposure is not None else "—", size=8,
                      color=BODY_TEXT, alignment=PP_ALIGN.RIGHT)
            _set_cell(tbl.cell(i + 1, 4), f.get("rule_id", "—"), size=7, color=GREY)
            if i % 2 == 0:
                for j in range(5):
                    tbl.cell(i + 1, j).fill.solid()
                    tbl.cell(i + 1, j).fill.fore_color.rgb = LIGHT_BG
        _footer(slide, run_id=state.run_id, generated_at=now)


def _build_test_coverage(prs, catalogue_rows: list[dict], state: RunState, now: str):
    catalogue_ids = {t["test_id"] for t in catalogue_rows if t.get("test_id")}
    rows = []
    for t in catalogue_rows:
        results = results_for(t["test_id"], state.test_results)
        combined = combined_status(results)
        status = combined.get("status", "not_testable")
        exceptions = combined.get("exception_units") if status == "exception" else None
        rows.append({
            "Test ID": t["test_id"], "Category": t.get("category", "—"),
            "Test Name": t.get("test_name", "—"), "Exceptions": exceptions, "Status": status,
        })

    n_slides = max(1, math.ceil(len(rows) / COVERAGE_ROWS_PER_SLIDE))
    for s in range(n_slides):
        chunk = rows[s * COVERAGE_ROWS_PER_SLIDE:(s + 1) * COVERAGE_ROWS_PER_SLIDE]
        title = "Test Coverage" if n_slides == 1 else f"Test Coverage ({s + 1}/{n_slides})"
        slide = _new_slide(prs, LY_TITLE_ONLY, title)
        n_rows = len(chunk) + 1
        tbl = slide.shapes.add_table(n_rows, 5, Inches(CONTENT_LEFT), Inches(1.15),
                                      Inches(CONTENT_W), Inches(min(0.32 * n_rows, 5.7))).table
        tbl.columns[0].width = Inches(1.0)
        tbl.columns[1].width = Inches(1.8)
        tbl.columns[2].width = Inches(5.8)
        tbl.columns[3].width = Inches(1.8)
        tbl.columns[4].width = Inches(1.98)
        headers = ["Test ID", "Category", "Test Name", "Exceptions", "Status"]
        for j, h in enumerate(headers):
            cell = tbl.cell(0, j)
            _set_cell(cell, h, size=9, bold=True, color=WHITE)
            cell.fill.solid()
            cell.fill.fore_color.rgb = TEAL
        for i, row in enumerate(chunk):
            _set_cell(tbl.cell(i + 1, 0), row["Test ID"], size=9, bold=True, color=TEAL)
            _set_cell(tbl.cell(i + 1, 1), row["Category"], size=8, color=GREY)
            _set_cell(tbl.cell(i + 1, 2), row["Test Name"], size=9, color=BODY_TEXT)
            exceptions = row["Exceptions"]
            _set_cell(tbl.cell(i + 1, 3), _count0(exceptions) if exceptions is not None else "—", size=9,
                      color=BODY_TEXT, alignment=PP_ALIGN.RIGHT)
            status = row["Status"]
            _set_cell(tbl.cell(i + 1, 4), STATUS_LABEL.get(status, status), size=9, bold=True,
                      color=STATUS_COLOR.get(status, GREY), alignment=PP_ALIGN.CENTER)
            if i % 2 == 0:
                for j in range(5):
                    tbl.cell(i + 1, j).fill.solid()
                    tbl.cell(i + 1, j).fill.fore_color.rgb = LIGHT_BG
        _footer(slide, run_id=state.run_id, generated_at=now)


# CLAUDE.md §4.7 defect 2: the methodology body is a fixed set of bullets
# (never open-ended user text), but its per-source reconciliation and
# not-testable-reason lines grow with the Skill's own contract -- budgeted
# generously for the ~2.4in x 5.5in body box at 10-11pt.
_METHODOLOGY_LINE_MAX_CHARS = 260


def _build_methodology(prs, state: RunState, metrics: dict, skill, catalogue_rows: list[dict], now: str):
    slide = _new_slide(prs, LY_TITLE_ONLY, "Methodology & Limitations")

    reconciliation = state.reconciliation or {}
    recon_ok = sum(1 for rec in reconciliation.values() if _reconciliation_ok(rec))
    recon_line = (
        f"{recon_ok}/{len(reconciliation)} source(s) reconciled with zero variance."
        if reconciliation else "No reconciliation recorded for this run."
    )
    if recon_ok != len(reconciliation):
        bad = sorted(s for s, rec in reconciliation.items() if not _reconciliation_ok(rec))
        recon_line += f" Variance detected: {', '.join(bad)}."

    thresholds = getattr(skill, "thresholds", {}) or {}
    n_analyst_set = sum(
        1 for spec in thresholds.values() if (spec.get("provenance") or {}).get("pending_policy_confirmation")
    )
    threshold_line = (
        f"{len(thresholds)} threshold(s) configured for this Skill; {n_analyst_set} are analyst-set and "
        f"pending policy confirmation — labelled wherever they drive a finding's severity."
    )

    not_testable = [t for t in state.test_results if t.get("status") == "not_testable"]
    if not_testable:
        parts = [f"{t['test_id']} ({t.get('reason', 'no reason recorded')})" for t in not_testable]
        not_testable_line = _ellipsize("Not testable: " + "; ".join(parts), _METHODOLOGY_LINE_MAX_CHARS)
    else:
        not_testable_line = "Every test in this run's plan was testable."

    namesake_rows = metrics.get("exco_namesake_excluded_claims_rows")
    namesake_amount = metrics.get("exco_namesake_excluded_claims_amount")
    if namesake_rows is not None:
        namesake_line = (
            f"Employee ID 50040 (\"Delacroix, Marie\", a namesake of ExCo member 52472) is excluded "
            f"from the ExCo population: {_count0(namesake_rows['value'])} claim(s), "
            f"{_money0((namesake_amount or {}).get('value'))}."
        )
    else:
        namesake_line = "Namesake-exclusion metric not recorded for this run."

    content = [
        ("Data sources", f"{len(state.data_assets)} bound source(s) at pinned table version / file hash. "
                          f"The full source-version and file-hash provenance is recorded with this run."),
        ("Population reconciliation", recon_line),
        ("Threshold provenance", threshold_line),
        ("Test coverage", f"{len(catalogue_rows)} catalogue test(s). {not_testable_line}"),
        ("Population scope", namesake_line),
        ("Computation", "Every number is computed deterministically in Python from the bound sources — "
                         "no LLM inference is used for metric calculation or finding selection."),
        ("Sign-off", _signoff_line(state)),
    ]
    tf = None
    for title, body in content:
        if tf is None:
            tf = _textbox(slide, CONTENT_LEFT, 1.1, CONTENT_W, 5.6, title, size=12, bold=True, color=TEAL,
                           name=FONT_HEAVY)
            _add_para(tf, body, size=10, color=BODY_TEXT, space_before=3)
        else:
            _add_para(tf, title, size=12, bold=True, color=TEAL, space_before=10, name=FONT_HEAVY)
            _add_para(tf, body, size=10, color=BODY_TEXT, space_before=3)
    _footer(slide, run_id=state.run_id, generated_at=now)


def _signoff_line(state: RunState) -> str:
    signoff = state.signoff or {}
    if not signoff:
        return "Not yet signed off."
    approver = signoff.get("approver", "—")
    # P7 review workflow (§3.7): "Prepared by P · Reviewed by R · Signed off
    # by A on T" once a signoff carries prepared_by; a legacy signoff keeps
    # its original one-part sentence, verbatim.
    if signoff.get("prepared_by") is not None:
        line = (
            f"Prepared by {signoff.get('prepared_by')} · Reviewed by {signoff.get('reviewed_by')} · "
            f"Signed off by {approver} on {signoff.get('timestamp', '—')}"
        )
    else:
        line = f"Signed off by {approver} on {signoff.get('timestamp', '—')}"
    if signoff.get("self_approved"):
        return f"{line} — {SELF_APPROVED_LABEL}."
    return f"{line}."


def _build_end(prs, state: RunState, now: str):
    slide = _new_slide(prs, LY_END, "Thank you")
    _footer(slide, run_id=state.run_id, generated_at=now, color=WHITE)


# ── main entry point ─────────────────────────────────────────────────────────


def generate_pptx(
    state: RunState,
    findings: list[dict],
    metrics: dict[str, dict],
    catalogue_rows: list[dict],
    skill,
    *,
    data_mode: str,
    template_path: str | Path,
    now: str,
    narration: dict | None = None,
    code_revision_note: str | None = None,
) -> bytes:
    """Builds the full PPTX audit pack and returns bytes. `skill` is the
    Skill this run used (orchestrator.skills.Skill) -- its `manifest` and
    `thresholds` feed the cover and methodology slides; `catalogue_rows` is
    that Skill's own catalogue.yaml (`load_catalogue_rows`), static per
    Skill, never per-run data. `findings` is already this run's own EFFECTIVE
    finding set -- `orchestrator.nodes.fieldwork._build_export_narration`
    (P6 WP N11, docs/specs/P6_narration_design.md §9) resolves every
    observation/recommendation/management_questions through
    `orchestrator.narration.resolve.effective_prose` before calling this
    function, so every slide below that reads `finding.get('observation')`
    etc. needs no narration-awareness of its own. `narration` carries
    everything ELSE this run's own reviewed narration supplies: the exec
    summary paragraphs and its label, confirmed themes (falling back to the
    fixed catalogue grouping when there are none), accepted AI-proposed
    findings, and the one chart caption. `None` (no caller outside
    `orchestrator.nodes.fieldwork.export` passes it) degrades to "nothing
    narrated" -- every slide below still renders its own reviewed
    deterministic fallback, labelled. `code_revision_note` (CLAUDE.md §11
    "Paused runs across a code deploy") is the cover-slide line stating
    "Computed under code revision X, exported under Y" -- `None` (the
    common case) adds no line at all."""
    narration = narration or {}
    template_path = Path(template_path)
    if not template_path.is_file():
        raise FileNotFoundError(
            f"PPTX template not found at {template_path} (PPTX_TEMPLATE_PATH / "
            f"orchestrator.config.DEFAULT_PPTX_TEMPLATE_PATH) -- run "
            f"scripts/strip_pptx_template.py to generate it"
        )
    prs = Presentation(str(template_path))
    assert len(prs.slides._sldIdLst) == 0, (
        f"{template_path} carries content slides -- it must be the stripped, content-free "
        f"template (scripts/strip_pptx_template.py), never the source deck"
    )

    # Independent review 2026-09-24 item 8 (D9): the CATALOGUE's own test
    # count, the same number the Test Coverage and Methodology slides
    # already state -- never a count of plan.yaml's own primitive
    # INSTANCES, which is a different, larger number for a Skill (like
    # SKILL-001) whose plan runs several sub-tests per catalogue test
    # (T3.2a_air_dom/_air_int/_car_dom/... under the one catalogue test
    # T3.2a). "This run assessed 21 deterministic test(s)" against a
    # 14-test catalogue was exactly that miscount.
    n_catalogue_tests = len(catalogue_rows)

    _build_cover(prs, state, skill.manifest, data_mode, now, code_revision_note)
    _build_exec_summary(prs, state, findings, metrics, n_catalogue_tests, now, narration)
    _build_what_we_found(prs, findings, catalogue_rows, state, now, narration)

    if not findings:
        _build_no_findings(prs, state, now)
    else:
        top = findings[:TOP_MATTERS_MAX]
        for rank, f in enumerate(top, start=1):
            _build_top_matter(prs, f, metrics, state, now, rank, len(top))

    _build_risk_and_exposure(prs, findings, metrics, state, now, narration)
    _build_all_findings_appendix(prs, findings, state, now)
    _build_test_coverage(prs, catalogue_rows, state, now)
    _build_methodology(prs, state, metrics, skill, catalogue_rows, now)
    _build_end(prs, state, now)

    actual = len(prs.slides._sldIdLst)
    expected = expected_slide_count(len(findings), len(catalogue_rows))
    assert actual == expected, f"slide count {actual} != expected_slide_count() {expected}"

    buf = io.BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf.getvalue()
