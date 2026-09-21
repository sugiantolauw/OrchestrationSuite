"""Excel Export — exception-level transaction data.

Generates an Excel workbook with:
  1. Summary sheet — finding titles, risk, exception count
  2. One sheet per finding — flagged transactions with full detail
  3. Test catalogue sheet
"""

import io
from datetime import datetime

import pandas as pd
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter


NAVY_FILL = PatternFill(start_color="1E2761", end_color="1E2761", fill_type="solid")
HEADER_FONT = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
CELL_FONT = Font(name="Calibri", size=10)
GREY_FONT = Font(name="Calibri", size=9, color="6B7283")
RED_FONT = Font(name="Calibri", size=10, bold=True, color="B85042")
GREEN_FONT = Font(name="Calibri", size=10, bold=True, color="2C7A4B")
AMBER_FONT = Font(name="Calibri", size=10, bold=True, color="E0952A")
ALT_FILL = PatternFill(start_color="F9FAFB", end_color="F9FAFB", fill_type="solid")
THIN_BORDER = Border(
    bottom=Side(style="thin", color="E4E7EE"),
)

RISK_FONT = {"High": RED_FONT, "Medium": AMBER_FONT, "Low": GREEN_FONT}


def _style_header(ws, n_cols):
    for col in range(1, n_cols + 1):
        cell = ws.cell(row=1, column=col)
        cell.fill = NAVY_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="left", vertical="center")


def _auto_width(ws, max_width=50):
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                val = str(cell.value) if cell.value else ""
                max_len = max(max_len, len(val))
            except Exception:
                pass
        ws.column_dimensions[col_letter].width = min(max_len + 3, max_width)


def _get_exception_df(finding, df_combined):
    """Filter the combined DataFrame to rows matching the finding's flag columns."""
    from src.test_catalogue import TEST_CATALOGUE

    test_id = finding.get("test_id", "")
    test_def = next((t for t in TEST_CATALOGUE if t["test_id"] == test_id), None)

    if test_def is None or test_def.get("flag_col") is None:
        return pd.DataFrame()  # No flag-level drill-down available

    flag_cols = test_def["flag_col"]
    if isinstance(flag_cols, str):
        flag_cols = [flag_cols]

    # Filter rows where ANY flag is 1
    available_flags = [c for c in flag_cols if c in df_combined.columns]
    if not available_flags:
        return pd.DataFrame()

    mask = df_combined[available_flags].sum(axis=1) > 0
    df_flagged = df_combined[mask].copy()

    # Select useful columns
    display_cols = [
        "Employee", "Transaction Date", "Expense Type", "Vendor",
        "Expense Amount (reimbursement currency)", "Source_Population",
    ] + available_flags
    display_cols = [c for c in display_cols if c in df_flagged.columns]

    return df_flagged[display_cols].sort_values(
        "Expense Amount (reimbursement currency)", ascending=False
    ) if "Expense Amount (reimbursement currency)" in display_cols else df_flagged[display_cols]


def generate_excel(findings, payload, catalogue_rows, df_combined):
    """Generate an Excel workbook with findings, exceptions, and test catalogue."""
    buf = io.BytesIO()

    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        # ── Summary sheet ────────────────────────────────────────────────
        summary_data = []
        for f in findings:
            summary_data.append({
                "Test ID": f["test_id"],
                "Risk": f["risk"],
                "Title": f["title"],
                "Observation": f["observation"],
                "Recommendation": f.get("recommendation", ""),
                "Metrics Cited": ", ".join(f.get("metrics_cited", [])),
            })
        df_summary = pd.DataFrame(summary_data)
        df_summary.to_excel(writer, sheet_name="Findings Summary", index=False)
        ws = writer.sheets["Findings Summary"]
        _style_header(ws, len(df_summary.columns))
        # Apply risk colour
        for row in range(2, len(df_summary) + 2):
            risk_cell = ws.cell(row=row, column=2)
            risk_cell.font = RISK_FONT.get(risk_cell.value, CELL_FONT)
        _auto_width(ws)

        # ── Exception sheets per finding ──────────────────────────────────
        for f in findings:
            df_exc = _get_exception_df(f, df_combined)
            if df_exc.empty:
                continue
            sheet_name = f"{f['test_id']}_{f['title'][:20]}".replace("/", "-").replace(":", "")[:31]
            df_exc.to_excel(writer, sheet_name=sheet_name, index=False)
            ws_exc = writer.sheets[sheet_name]
            _style_header(ws_exc, len(df_exc.columns))
            # Alternate row shading
            for row in range(2, len(df_exc) + 2):
                if row % 2 == 0:
                    for col in range(1, len(df_exc.columns) + 1):
                        ws_exc.cell(row=row, column=col).fill = ALT_FILL
            _auto_width(ws_exc)

        # ── Test Catalogue sheet ──────────────────────────────────────────
        df_cat = pd.DataFrame(catalogue_rows)
        df_cat.to_excel(writer, sheet_name="Test Catalogue", index=False)
        ws_cat = writer.sheets["Test Catalogue"]
        _style_header(ws_cat, len(df_cat.columns))
        # Status colours
        status_col_idx = list(df_cat.columns).index("Status") + 1 if "Status" in df_cat.columns else None
        if status_col_idx:
            for row in range(2, len(df_cat) + 2):
                cell = ws_cat.cell(row=row, column=status_col_idx)
                if cell.value == "Exception":
                    cell.font = RED_FONT
                elif cell.value == "Pass":
                    cell.font = GREEN_FONT
                else:
                    cell.font = GREY_FONT
        _auto_width(ws_cat)

        # ── Metadata sheet ────────────────────────────────────────────────
        meta = {
            "Property": [
                "Report Generated", "Audit Period", "Total Claims",
                "Total Spend (AUD)", "ExCo Members", "Source Files",
                "Findings Count", "App Version",
            ],
            "Value": [
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                payload.get("audit_period", "N/A"),
                payload.get("total_claims", 0),
                f"${payload.get('total_spend', 0):,.0f}",
                payload.get("exco_members_count", 0),
                len(payload.get("source_files", {})),
                len(findings),
                "1.0.0",
            ],
        }
        df_meta = pd.DataFrame(meta)
        df_meta.to_excel(writer, sheet_name="Run Metadata", index=False)
        ws_meta = writer.sheets["Run Metadata"]
        _style_header(ws_meta, 2)
        _auto_width(ws_meta)

    buf.seek(0)
    return buf.getvalue()
