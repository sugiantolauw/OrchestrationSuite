"""PPTX Audit Pack Generator — Optus branded.

Uses the Optus corporate template (assets/optus_template.pptx) to produce
a branded slide deck with:

  EXECUTIVE SECTION
    1. Cover slide (Cover Slide Teal layout)
    2. Executive summary — KPIs, risk breakdown, top matters
    3. Test coverage heatmap
    4. High-risk findings (1 slide each)

  DETAILED APPENDIX (for business-unit audience)
    5. Section divider — "Detailed Findings"
    6. All Medium/Low findings (1 slide each)
    7. Methodology & limitations

  CLOSE
    8. End slide (branded)
"""

import io
import os
from datetime import datetime
from pathlib import Path

from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE
from pptx.chart.data import CategoryChartData, ChartData
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION, XL_LABEL_POSITION

# ── Template path ────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = _HERE / "assets" / "optus_template.pptx"

# ── Optus brand colours ─────────────────────────────────────────────────────
TEAL       = RGBColor(0x00, 0x62, 0x80)
DARK_TEAL  = RGBColor(0x00, 0x32, 0x3A)
BODY_TEXT  = RGBColor(0x1A, 0x1A, 0x1A)
WHITE      = RGBColor(0xFF, 0xFF, 0xFF)
LIGHT_BG   = RGBColor(0xF5, 0xF6, 0xF8)
GREY       = RGBColor(0x6B, 0x72, 0x83)
LIGHT_GREY = RGBColor(0xE4, 0xE7, 0xEE)
RED        = RGBColor(0xB8, 0x50, 0x42)
AMBER      = RGBColor(0xE0, 0x95, 0x2A)
GREEN      = RGBColor(0x2C, 0x7A, 0x4B)

RISK_COLOR   = {"High": RED, "Medium": AMBER, "Low": GREEN}
STATUS_COLOR = {"Exception": RED, "Pass": GREEN, "Not testable": GREY}

# Fonts — the template uses MarkOT family; fall back to Calibri
FONT_HEAVY = "MarkOT-Heavy"
FONT_BODY  = "MarkOT"

# ── Layout indices in the Optus template ─────────────────────────────────────
LY_COVER       = 3   # "Cover Slide Teal" — idx0=CENTER_TITLE, idx1=SUBTITLE, idx16=BODY
LY_DIVIDER     = 6   # "Divider Slide White - Large Font" — idx0=TITLE
LY_TITLE_ONLY  = 35  # "Title Only" — idx0=TITLE, idx16=FOOTER, idx15=BODY
LY_BLANK       = 34  # "Blank Slide" — idx16=FOOTER, idx15=BODY
LY_END         = 57  # "End Slide Teal" — idx0=TITLE

# Slide dimensions (13.33" x 7.50")
SLIDE_W = 13.33
SLIDE_H = 7.50
CONTENT_LEFT = 0.55
CONTENT_RIGHT = 12.78
CONTENT_W = CONTENT_RIGHT - CONTENT_LEFT
TITLE_TOP = 0.39


# ── Helpers ──────────────────────────────────────────────────────────────────

def _font(run, size=11, bold=False, color=BODY_TEXT, name=None):
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = color
    run.font.name = name or FONT_BODY


def _set_cell(cell, text, size=10, bold=False, color=BODY_TEXT, alignment=PP_ALIGN.LEFT):
    cell.text = ""
    p = cell.text_frame.paragraphs[0]
    p.alignment = alignment
    run = p.add_run()
    run.text = str(text)
    _font(run, size, bold, color)
    cell.vertical_anchor = MSO_ANCHOR.MIDDLE


def _add_text(slide, left, top, width, height, text,
              size=11, bold=False, color=BODY_TEXT, alignment=PP_ALIGN.LEFT, name=None):
    txBox = slide.shapes.add_textbox(Inches(left), Inches(top),
                                     Inches(width), Inches(height))
    tf = txBox.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.alignment = alignment
    run = p.add_run()
    run.text = text
    _font(run, size, bold, color, name)
    return tf


def _add_para(tf, text, size=11, bold=False, color=BODY_TEXT, space_before=0, name=None):
    p = tf.add_paragraph()
    p.space_before = Pt(space_before)
    run = p.add_run()
    run.text = text
    _font(run, size, bold, color, name)
    return p


def _kpi_box(slide, left, top, label, value, accent=TEAL):
    box = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE,
        Inches(left), Inches(top), Inches(2.3), Inches(1.0),
    )
    box.fill.solid()
    box.fill.fore_color.rgb = LIGHT_BG
    box.line.color.rgb = LIGHT_GREY
    box.line.width = Pt(0.75)
    _add_text(slide, left + 0.15, top + 0.08, 2.0, 0.3,
              label, size=9, color=GREY, bold=True)
    _add_text(slide, left + 0.15, top + 0.42, 2.0, 0.5,
              value, size=22, bold=True, color=accent, name=FONT_HEAVY)


def _risk_badge(slide, left, top, risk_text):
    color = RISK_COLOR.get(risk_text, GREY)
    badge = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE,
        Inches(left), Inches(top), Inches(1.1), Inches(0.35),
    )
    badge.fill.solid()
    badge.fill.fore_color.rgb = color
    badge.line.fill.background()
    badge.text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER
    run = badge.text_frame.paragraphs[0].add_run()
    run.text = risk_text
    _font(run, 10, True, WHITE)


def _section_label(slide, left, top, text):
    _add_text(slide, left, top, CONTENT_W, 0.25,
              text, size=9, bold=True, color=TEAL, name=FONT_HEAVY)


# ── Slide builders ───────────────────────────────────────────────────────────

def _build_cover(prs, payload):
    layout = prs.slide_layouts[LY_COVER]
    slide = prs.slides.add_slide(layout)

    title_ph = slide.placeholders[0]
    title_ph.text = "Travel & Entertainment"
    for run in title_ph.text_frame.paragraphs[0].runs:
        _font(run, 36, True, WHITE, FONT_HEAVY)

    sub_ph = slide.placeholders[1]
    sub_ph.text = "Executive Diligence — Audit Analytics Report"
    for run in sub_ph.text_frame.paragraphs[0].runs:
        _font(run, 18, False, WHITE, FONT_BODY)

    detail_ph = slide.placeholders[16]
    period = payload.get("audit_period", "N/A")
    detail_ph.text = (
        f"For Chief Internal Auditor  ·  {datetime.now().strftime('%d %B %Y')}  ·  "
        f"Period: {period}"
    )
    for run in detail_ph.text_frame.paragraphs[0].runs:
        _font(run, 12, False, WHITE, FONT_BODY)


def _build_exec_summary(prs, payload, findings, catalogue_rows):
    slide = prs.slides.add_slide(prs.slide_layouts[LY_TITLE_ONLY])
    slide.placeholders[0].text = "Executive Summary"

    n_high = sum(1 for f in findings if f["risk"] == "High")
    n_med  = sum(1 for f in findings if f["risk"] == "Medium")
    n_low  = sum(1 for f in findings if f["risk"] == "Low")
    n_exc  = sum(1 for r in catalogue_rows if r["Status"] == "Exception")
    n_pass = sum(1 for r in catalogue_rows if r["Status"] == "Pass")
    total_exposure = sum(f.get("financial_exposure", 0) for f in findings)

    kpis = [
        ("Total Spend",       f"${payload.get('total_spend', 0):,.0f}"),
        ("Claims Analysed",   f"{payload.get('total_claims', 0):,}"),
        ("Tests Executed",    f"{len(catalogue_rows)}"),
        ("Exceptions Found",  f"{n_exc}"),
        ("Findings Raised",   f"{len(findings)}"),
    ]
    for i, (label, value) in enumerate(kpis):
        _kpi_box(slide, CONTENT_LEFT + i * 2.45, 1.25, label, value)

    _section_label(slide, CONTENT_LEFT, 2.55, "FINDINGS BY RISK RATING")
    risk_items = [(f"{n_high} High", RED), (f"{n_med} Medium", AMBER), (f"{n_low} Low", GREEN)]
    for j, (txt, clr) in enumerate(risk_items):
        _add_text(slide, CONTENT_LEFT + j * 2.0, 2.85, 1.8, 0.3,
                  txt, size=14, bold=True, color=clr, name=FONT_HEAVY)

    if total_exposure:
        _add_text(slide, CONTENT_LEFT + 6.5, 2.85, 4.0, 0.3,
                  f"Total financial exposure: ${total_exposure:,.0f}",
                  size=13, bold=True, color=RED, name=FONT_HEAVY)

    _section_label(slide, CONTENT_LEFT, 3.45, "TEST COVERAGE")
    n_na = sum(1 for r in catalogue_rows if r["Status"] == "Not testable")
    cov_items = [(f"{n_exc} Exception", RED), (f"{n_pass} Pass", GREEN), (f"{n_na} Not testable", GREY)]
    for j, (txt, clr) in enumerate(cov_items):
        _add_text(slide, CONTENT_LEFT + j * 2.5, 3.75, 2.3, 0.3,
                  txt, size=13, bold=True, color=clr, name=FONT_HEAVY)

    _section_label(slide, CONTENT_LEFT, 4.45, "TOP MATTERS FOR MANAGEMENT ATTENTION")
    high = [f for f in findings if f["risk"] == "High"] or findings[:3]
    tf = _add_text(slide, CONTENT_LEFT, 4.80, CONTENT_W, 2.2, "", size=12, color=BODY_TEXT)
    for i, f in enumerate(high[:5]):
        bullet = f"{i+1}.  [{f['risk']}]  {f['test_id']}: {f['title']}"
        _add_para(tf, bullet, size=12, color=BODY_TEXT, space_before=6)


def _build_test_coverage(prs, catalogue_rows):
    slide = prs.slides.add_slide(prs.slide_layouts[LY_TITLE_ONLY])
    slide.placeholders[0].text = "Test Coverage Summary"

    rows_data = catalogue_rows
    n_rows = len(rows_data) + 1
    n_cols = 5
    tbl_shape = slide.shapes.add_table(
        n_rows, n_cols,
        Inches(CONTENT_LEFT), Inches(1.15),
        Inches(CONTENT_W), Inches(min(0.32 * n_rows, 5.8)),
    )
    table = tbl_shape.table

    table.columns[0].width = Inches(1.0)
    table.columns[1].width = Inches(1.8)
    table.columns[2].width = Inches(5.8)
    table.columns[3].width = Inches(1.8)
    table.columns[4].width = Inches(1.8)

    headers = ["Test ID", "Category", "Test Name", "Exceptions", "Status"]
    for j, h in enumerate(headers):
        cell = table.cell(0, j)
        _set_cell(cell, h, size=9, bold=True, color=WHITE)
        cell.fill.solid()
        cell.fill.fore_color.rgb = TEAL

    for i, row in enumerate(rows_data):
        _set_cell(table.cell(i+1, 0), row["Test ID"], size=9, bold=True, color=TEAL)
        _set_cell(table.cell(i+1, 1), row["Category"], size=8, color=GREY)
        _set_cell(table.cell(i+1, 2), row["Test Name"], size=9, color=BODY_TEXT)
        _set_cell(table.cell(i+1, 3),
                  f"{row['Exceptions']:,}" if row["Exceptions"] else "—",
                  size=9, color=BODY_TEXT, alignment=PP_ALIGN.RIGHT)
        status = row["Status"]
        _set_cell(table.cell(i+1, 4), status, size=9, bold=True,
                  color=STATUS_COLOR.get(status, GREY), alignment=PP_ALIGN.CENTER)
        if i % 2 == 0:
            for j in range(n_cols):
                table.cell(i+1, j).fill.solid()
                table.cell(i+1, j).fill.fore_color.rgb = LIGHT_BG


def _build_finding_slide(prs, finding, payload_metrics):
    slide = prs.slides.add_slide(prs.slide_layouts[LY_TITLE_ONLY])
    risk = finding["risk"]

    slide.placeholders[0].text = f"{finding['test_id']}: {finding['title']}"

    _risk_badge(slide, CONTENT_LEFT, 1.15, risk)

    score = finding.get("risk_score", 0)
    exposure = finding.get("financial_exposure", 0)
    exc_count = finding.get("exception_count", 0)
    chip_parts = []
    if score:
        chip_parts.append(f"Risk score: {score}")
    if exposure:
        chip_parts.append(f"Exposure: ${exposure:,.0f}")
    if exc_count:
        chip_parts.append(f"Exceptions: {exc_count:,}")
    if chip_parts:
        _add_text(slide, CONTENT_LEFT + 1.3, 1.17, 6.0, 0.3,
                  "  ·  ".join(chip_parts), size=10, color=GREY)

    _section_label(slide, CONTENT_LEFT, 1.70, "OBSERVATION")
    _add_text(slide, CONTENT_LEFT, 1.95, CONTENT_W * 0.65, 1.2,
              finding["observation"], size=11, color=BODY_TEXT)

    _section_label(slide, CONTENT_LEFT, 3.25, "RECOMMENDATION")
    _add_text(slide, CONTENT_LEFT, 3.50, CONTENT_W * 0.65, 1.0,
              finding.get("recommendation", ""), size=11, color=BODY_TEXT)

    metrics_cited = finding.get("metrics_cited", [])
    if metrics_cited:
        metrics_left = CONTENT_LEFT + CONTENT_W * 0.68
        metrics_w = CONTENT_W * 0.32
        _section_label(slide, metrics_left, 1.70, "METRICS CITED")

        n_m = min(len(metrics_cited), 8)
        tbl = slide.shapes.add_table(
            n_m + 1, 3,
            Inches(metrics_left), Inches(2.0),
            Inches(metrics_w), Inches(0.28 * (n_m + 1)),
        ).table
        tbl.columns[0].width = Inches(metrics_w * 0.40)
        tbl.columns[1].width = Inches(metrics_w * 0.25)
        tbl.columns[2].width = Inches(metrics_w * 0.35)

        for j, h in enumerate(["Metric", "Value", "Source"]):
            cell = tbl.cell(0, j)
            _set_cell(cell, h, size=8, bold=True, color=WHITE)
            cell.fill.solid()
            cell.fill.fore_color.rgb = TEAL

        for i, mid in enumerate(metrics_cited[:8]):
            m = payload_metrics.get(mid, {})
            val = m.get("value", "—")
            unit = m.get("unit", "")
            src = m.get("source_file", "—")
            if unit == "AUD":
                val_str = f"${val:,.0f}" if isinstance(val, (int, float)) else str(val)
            elif unit == "%":
                val_str = f"{val}%" if isinstance(val, (int, float)) else str(val)
            else:
                val_str = f"{val:,}" if isinstance(val, (int, float)) else str(val)
            _set_cell(tbl.cell(i+1, 0), mid, size=7, color=GREY)
            _set_cell(tbl.cell(i+1, 1), val_str, size=8, bold=True,
                      color=BODY_TEXT, alignment=PP_ALIGN.RIGHT)
            _set_cell(tbl.cell(i+1, 2), src, size=7, color=GREY)

    questions = finding.get("management_questions", [])
    if questions:
        q_top = 4.65
        _section_label(slide, CONTENT_LEFT, q_top, "MANAGEMENT QUESTIONS")
        qbox = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE,
            Inches(CONTENT_LEFT), Inches(q_top + 0.25),
            Inches(CONTENT_W * 0.65), Inches(min(len(questions) * 0.32 + 0.15, 2.2)),
        )
        qbox.fill.solid()
        qbox.fill.fore_color.rgb = LIGHT_BG
        qbox.line.color.rgb = LIGHT_GREY
        qbox.line.width = Pt(0.5)

        tf = _add_text(slide, CONTENT_LEFT + 0.15, q_top + 0.32,
                       CONTENT_W * 0.65 - 0.3, 2.0, "", size=10, color=BODY_TEXT)
        for q in questions[:5]:
            _add_para(tf, f"•  {q}", size=10, color=BODY_TEXT, space_before=3)


def _build_divider(prs, title_text):
    slide = prs.slides.add_slide(prs.slide_layouts[LY_DIVIDER])
    slide.placeholders[0].text = title_text


def _build_methodology(prs, payload):
    slide = prs.slides.add_slide(prs.slide_layouts[LY_TITLE_ONLY])
    slide.placeholders[0].text = "Methodology & Limitations"

    content = [
        ("Data Sources",
         f"{len(payload.get('source_files', {}))} source files covering "
         f"{payload.get('months_covered', 0)} months ({payload.get('audit_period', 'N/A')})."),
        ("Population",
         f"{payload.get('total_claims', 0):,} expense claims totalling "
         f"${payload.get('total_spend', 0):,.0f} for "
         f"{payload.get('exco_members_count', 0)} ExCo members."),
        ("Computation",
         "All metrics are computed deterministically from source data using Python. "
         "No large-language-model inference is used for metric calculation."),
        ("Traceability",
         "Every metric links to its source data file. The evidence drawer shows "
         "metric ID \u2192 computed value \u2192 source file for each finding."),
        ("Guardrails",
         "Displayed values are recomputed from the source population at runtime. "
         "This proves calculation consistency but does not validate the completeness "
         "or accuracy of the underlying source data."),
        ("Limitations",
         "\u2022 Source data completeness has not been independently verified.\n"
         "\u2022 Policy thresholds are hard-coded and may require periodic review.\n"
         "\u2022 Personal-expense flags are heuristic-based and require auditor judgement.\n"
         "\u2022 The app does not persist state between sessions."),
    ]

    tf = _add_text(slide, CONTENT_LEFT, 1.15, CONTENT_W, 5.5,
                   "", size=11, color=BODY_TEXT)
    for title, body in content:
        _add_para(tf, title, size=12, bold=True, color=TEAL, space_before=12, name=FONT_HEAVY)
        _add_para(tf, body, size=10, color=BODY_TEXT, space_before=3)


def _build_risk_chart(prs, findings):
    """Donut chart showing findings by risk level."""
    slide = prs.slides.add_slide(prs.slide_layouts[LY_TITLE_ONLY])
    slide.placeholders[0].text = "Risk Distribution"

    n_high = sum(1 for f in findings if f["risk"] == "High")
    n_med  = sum(1 for f in findings if f["risk"] == "Medium")
    n_low  = sum(1 for f in findings if f["risk"] == "Low")

    chart_data = ChartData()
    chart_data.categories = ["High", "Medium", "Low"]
    chart_data.add_series("Findings", (n_high, n_med, n_low))

    chart_frame = slide.shapes.add_chart(
        XL_CHART_TYPE.DOUGHNUT, Inches(CONTENT_LEFT), Inches(1.3),
        Inches(5.5), Inches(5.5), chart_data,
    )
    chart = chart_frame.chart
    chart.has_legend = True
    chart.legend.position = XL_LEGEND_POSITION.BOTTOM
    chart.legend.include_in_layout = False
    chart.legend.font.size = Pt(11)

    plot = chart.plots[0]
    plot.has_data_labels = True
    plot.data_labels.font.size = Pt(12)
    plot.data_labels.font.bold = True
    plot.data_labels.number_format = '0'
    plot.data_labels.show_value = True
    plot.data_labels.show_category_name = True
    plot.data_labels.show_percentage = False

    series = plot.series[0]
    risk_colors = [RED, AMBER, GREEN]
    for i, color in enumerate(risk_colors):
        pt = series.points[i]
        pt.format.fill.solid()
        pt.format.fill.fore_color.rgb = color

    # Exposure summary on the right
    total_exp = sum(f.get("financial_exposure", 0) for f in findings)
    high_exp = sum(f.get("financial_exposure", 0) for f in findings if f["risk"] == "High")
    med_exp = sum(f.get("financial_exposure", 0) for f in findings if f["risk"] == "Medium")

    _section_label(slide, 6.5, 1.5, "FINANCIAL EXPOSURE SUMMARY")
    tf = _add_text(slide, 6.5, 1.85, 5.5, 3.5, "", size=11, color=BODY_TEXT)
    _add_para(tf, f"Total exposure: ${total_exp:,.0f}", size=16, bold=True, color=TEAL, space_before=8, name=FONT_HEAVY)
    _add_para(tf, f"High-risk: ${high_exp:,.0f}", size=13, bold=True, color=RED, space_before=12)
    _add_para(tf, f"Medium-risk: ${med_exp:,.0f}", size=13, bold=True, color=AMBER, space_before=6)
    _add_para(tf, "", size=8, space_before=16)
    _add_para(tf, f"{len(findings)} findings across {sum(1 for f in findings if f['risk'] == 'High')} high, "
              f"{sum(1 for f in findings if f['risk'] == 'Medium')} medium, "
              f"{sum(1 for f in findings if f['risk'] == 'Low')} low risk",
              size=11, color=GREY, space_before=4)


def _build_spend_chart(prs, payload):
    """Bar chart showing spend by expense type."""
    expense_breakdown = payload.get("expense_breakdown", {})
    if not expense_breakdown:
        return

    slide = prs.slides.add_slide(prs.slide_layouts[LY_TITLE_ONLY])
    slide.placeholders[0].text = "Spend by Expense Type"

    # Sort by value descending, take top 10
    sorted_types = sorted(expense_breakdown.items(), key=lambda x: x[1], reverse=True)[:10]
    categories = [t[0] for t in sorted_types]
    values = [t[1] for t in sorted_types]

    chart_data = CategoryChartData()
    chart_data.categories = categories
    chart_data.add_series("Spend ($)", values)

    chart_frame = slide.shapes.add_chart(
        XL_CHART_TYPE.BAR_CLUSTERED,
        Inches(CONTENT_LEFT), Inches(1.3),
        Inches(CONTENT_W), Inches(5.5),
        chart_data,
    )
    chart = chart_frame.chart
    chart.has_legend = False

    plot = chart.plots[0]
    plot.has_data_labels = True
    plot.data_labels.font.size = Pt(9)
    plot.data_labels.number_format = '$#,##0'
    plot.data_labels.show_value = True

    series = plot.series[0]
    series.format.fill.solid()
    series.format.fill.fore_color.rgb = TEAL

    chart.value_axis.visible = False
    chart.category_axis.tick_labels.font.size = Pt(10)


def _build_monthly_chart(prs, payload):
    """Line+bar chart showing monthly T&E volume."""
    monthly_data = payload.get("monthly_data", [])
    if not monthly_data:
        return

    slide = prs.slides.add_slide(prs.slide_layouts[LY_TITLE_ONLY])
    slide.placeholders[0].text = "Monthly T&E Volume"

    months = [m["month"] for m in monthly_data]
    claims = [m["claims"] for m in monthly_data]
    amounts = [m["amount"] for m in monthly_data]

    chart_data = CategoryChartData()
    chart_data.categories = months
    chart_data.add_series("Claims", claims)
    chart_data.add_series("Spend ($)", amounts)

    chart_frame = slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED,
        Inches(CONTENT_LEFT), Inches(1.3),
        Inches(CONTENT_W), Inches(5.5),
        chart_data,
    )
    chart = chart_frame.chart
    chart.has_legend = True
    chart.legend.position = XL_LEGEND_POSITION.BOTTOM
    chart.legend.font.size = Pt(11)

    plot = chart.plots[0]
    series_claims = plot.series[0]
    series_claims.format.fill.solid()
    series_claims.format.fill.fore_color.rgb = TEAL

    series_spend = plot.series[1]
    series_spend.format.fill.solid()
    series_spend.format.fill.fore_color.rgb = DARK_TEAL

    chart.category_axis.tick_labels.font.size = Pt(9)


def _build_end(prs):
    slide = prs.slides.add_slide(prs.slide_layouts[LY_END])
    slide.placeholders[0].text = "Thank you"


# ── Main entry point ─────────────────────────────────────────────────────────

def generate_pptx(payload, findings, catalogue_rows):
    """Generate the full PPTX audit pack and return bytes."""
    if TEMPLATE_PATH.exists():
        prs = Presentation(str(TEMPLATE_PATH))
        # Remove existing content slides from template
        _ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
        xml_slides = list(prs.slides._sldIdLst)
        for sldId in xml_slides:
            rId = sldId.get(f"{_ns}id")
            if rId is None:
                for attr_name, attr_val in sldId.attrib.items():
                    if attr_name.endswith("}id") or attr_name == "r:id":
                        rId = attr_val
                        break
            if rId:
                try:
                    prs.part.drop_rel(rId)
                except KeyError:
                    pass
            prs.slides._sldIdLst.remove(sldId)
    else:
        prs = Presentation()
        prs.slide_width = Inches(SLIDE_W)
        prs.slide_height = Inches(SLIDE_H)

    metrics = payload.get("metrics", {})

    # ── Executive section ────────────────────────────────────────────────
    _build_cover(prs, payload)
    _build_exec_summary(prs, payload, findings, catalogue_rows)
    _build_risk_chart(prs, findings)
    _build_spend_chart(prs, payload)
    _build_monthly_chart(prs, payload)
    _build_test_coverage(prs, catalogue_rows)

    # High-risk findings
    high_findings = [f for f in findings if f["risk"] == "High"]
    if high_findings:
        for f in high_findings:
            _build_finding_slide(prs, f, metrics)

    # ── Detailed appendix ────────────────────────────────────────────────
    _build_divider(prs, "Detailed Findings")

    med_low = [f for f in findings if f["risk"] in ("Medium", "Low")]
    for f in med_low:
        _build_finding_slide(prs, f, metrics)

    _build_methodology(prs, payload)

    # ── Close ────────────────────────────────────────────────────────────
    _build_end(prs)

    buf = io.BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf.getvalue()
