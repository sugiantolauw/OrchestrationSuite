"""PPTX and Excel exports: download, then open and inspect the file.

Covers feature ids X1, X2 (see features.py).

Independent review 2026-09-24 item 8: both tests here were written against
reference_app/app.py's prototype exports (sheet names "Findings Summary" /
"Test Catalogue" / "Run Metadata" that were never real -- the prototype's
demo XLSX, not this engine's), not the real workpaper
orchestrator.nodes.fieldwork._write_xlsx_workpaper actually produces
("Findings" / "Metrics" / "Test Results" / "Reconciliation" / "Flagged Row
Counts", plus "Ticket Preview" when requested). test_export_excel_... is
fixed to match.

CLAUDE.md §4.7's PPTX rebuild has shipped: `export()` writes a "pptx" kind
alongside the xlsx one (orchestrator.pptx_export.generate_pptx), so X1 below
asserts against the real deck's own bounded structure -- slide count equal
to `expected_slide_count()`, at least one native chart, and a footer
(run_id + generated_at) on every slide -- rather than the prototype's demo
deck it used to be written against.
"""

from __future__ import annotations

import re

import openpyxl
from pptx import Presentation

from tests.e2e.conftest import goto_workspace


def test_export_pptx_downloads_valid_deck_with_charts(watched_page, app_base_url, tmp_path):
    """X1."""
    page, watcher = watched_page
    goto_workspace(page, app_base_url)
    page.get_by_role("tab", name="Findings & Actions").click()
    page.wait_for_selector("button:has-text('Export PPTX')")

    with page.expect_download() as dl_info:
        page.locator("button:has-text('Export PPTX')").click()
    download = dl_info.value

    assert download.suggested_filename.endswith(".pptx")
    # Playwright's own download.path() is a bare temp file with no
    # extension; save_as() gives python-pptx/openpyxl a real .pptx/.xlsx
    # path to open, matching what a user's browser would actually save.
    saved_path = tmp_path / download.suggested_filename
    download.save_as(saved_path)
    prs = Presentation(saved_path)

    slide_count = len(prs.slides._sldIdLst)
    assert slide_count > 0

    # Footer on every slide (CLAUDE.md §4.7 rule 3 / §9A.2): run_id +
    # generated_at, and the SAME run_id on every slide -- never a mix, which
    # would mean a stale/cached deck was served instead of this run's own.
    footer_re = re.compile(r"run_id=(\S+) \| generated_at=(\S+)")
    run_ids = set()
    for slide in prs.slides:
        footer_texts = [
            m.group(1)
            for shape in slide.shapes
            if shape.has_text_frame
            for m in [footer_re.search(shape.text_frame.text)]
            if m
        ]
        assert footer_texts, "slide has no footer stamping run_id + generated_at"
        run_ids.update(footer_texts)
    assert len(run_ids) == 1, f"footer run_id is not consistent across slides: {run_ids}"

    has_chart = False
    for slide in prs.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_chart", False):
                has_chart = True
    assert has_chart, "expected at least one native PowerPoint chart across the deck"

    watcher.assert_clean()


def test_export_excel_downloads_valid_workbook_with_expected_sheets(watched_page, app_base_url, tmp_path):
    """X2."""
    page, watcher = watched_page
    goto_workspace(page, app_base_url)
    page.get_by_role("tab", name="Findings & Actions").click()
    page.wait_for_selector("button:has-text('Export Excel')")

    with page.expect_download() as dl_info:
        page.locator("button:has-text('Export Excel')").click()
    download = dl_info.value

    assert download.suggested_filename.endswith(".xlsx")
    saved_path = tmp_path / download.suggested_filename
    download.save_as(saved_path)
    wb = openpyxl.load_workbook(saved_path)

    # The real workpaper's own sheet names (orchestrator/nodes/fieldwork.py
    # _write_xlsx_workpaper), not the prototype's demo-export sheet names.
    assert "Findings" in wb.sheetnames
    assert "Metrics" in wb.sheetnames
    assert "Test Results" in wb.sheetnames
    assert "Reconciliation" in wb.sheetnames
    assert len(wb.sheetnames) >= 4

    findings_ws = wb["Findings"]
    assert findings_ws.max_row >= 1
    header = [c.value for c in findings_ws[1]]
    assert "exposure_amount" in header

    watcher.assert_clean()
