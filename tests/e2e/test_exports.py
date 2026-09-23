"""PPTX and Excel exports: download, then open and inspect the file.

Covers feature ids X1, X2 (see features.py).
"""

from __future__ import annotations

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

    assert "Findings Summary" in wb.sheetnames
    assert "Test Catalogue" in wb.sheetnames
    assert "Run Metadata" in wb.sheetnames
    assert len(wb.sheetnames) >= 3

    summary_ws = wb["Findings Summary"]
    assert summary_ws.max_row >= 1

    watcher.assert_clean()
