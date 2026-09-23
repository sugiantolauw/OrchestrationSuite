"""Generates the 8 SKILL-001 source files for Surface 2 (CLAUDE.md §5/§9): a
planted-exception fixture built from `plants.yaml` (the HAND-AUTHORED oracle
-- this module never decides which rows are exceptions, it only materialises
what `plants.yaml` already declares) plus a seeded, deterministic population
of background rows that are clearly non-exceptions.

Every generated file matches `skills/tne_exco/contract.yaml`'s declared file
name, sheet and columns for that source exactly, so it satisfies the same
contract the real Skill runs against. Two sources (`expense_report`,
`attendee_validity`) also carry one extra bookkeeping column, `Line Marker`
-- not part of the contract, ignored by contract validation and by every
primitive -- that gives a row-grain natural identity where the contract
alone does not (a report can have several lines; an entry can have several
attendee rows). `per_diem_rates` is not planted at all: it is copied
verbatim from the real reference file (CLAUDE.md task item 1).

Usage: `generate(plants_path, out_dir)` reads `plants.yaml` and writes all 8
sources into `out_dir`. Run as a script to (re)write the committed sample
under `data/`.
"""

from __future__ import annotations

import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import yaml

_THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = _THIS_DIR.parents[2]
SKILL_DIR = REPO_ROOT / "skills" / "tne_exco"
SYNTHETIC_DATA_DIR = REPO_ROOT / "synthetic_data"

DEFAULT_SEED = 20260923
AUDIT_START = date(2025, 1, 1)
AUDIT_END = date(2026, 4, 30)

_CONTRACT = yaml.safe_load((SKILL_DIR / "contract.yaml").read_text())["sources"]

EXCO_IDS: list[int] = yaml.safe_load((SKILL_DIR / "reference" / "exco_employee_ids.yaml").read_text())
EXCO_NAMES: list[str] = yaml.safe_load((SKILL_DIR / "reference" / "exco_display_names.yaml").read_text())
EXCO_ID_BY_NAME = dict(zip(EXCO_NAMES, EXCO_IDS))
EXCO_NAME_BY_ID = dict(zip(EXCO_IDS, EXCO_NAMES))

# Identities that never satisfy any population's ExCo filter -- the simplest,
# safest way to make a background row "clearly a non-exception" (CLAUDE.md
# §9) is to put it entirely outside every test's population, not to rely on
# getting every exception condition right for eleven real ExCo identities.
NON_EXCO: list[tuple[int, str]] = [
    (90001, "Alvarez, Diego"), (90002, "Patel, Nisha"), (90003, "Wong, Kelly"),
    (90004, "Novak, Petra"), (90005, "Haddad, Omar"), (90006, "Larsen, Freya"),
    (90007, "Ibrahim, Yusuf"), (90008, "Costa, Bruno"), (90009, "Singh, Manpreet"),
    (90010, "Dubois, Camille"), (90011, "Fischer, Lena"), (90012, "Osei, Kwame"),
]

# Expense types/vendors that do not belong to ANY test's targeted sub-population
# (T3.2a's four categories, T4.2/T3.3b's entertainment types, T6.1d's
# meals/incidentals types) -- filler for background rows that must stay inert.
FILLER_EXPENSE_TYPES = ["Taxis - Domestic Travel", "Mobile Phone Charges", "Office Supplies", "Parking - Domestic Travel"]
FILLER_VENDORS = ["City Cabs", "Telstra Corp", "Officeworks", "Wilson Parking", "Corner Cafe", "Ampol Fuel"]
FILLER_CITIES = ["Sydney", "Melbourne", "Brisbane", "Adelaide"]


def _rng(seed: int) -> random.Random:
    return random.Random(seed)


def _rand_date(rng: random.Random, start: date = AUDIT_START, end: date = AUDIT_END) -> date:
    span = (end - start).days
    return start + timedelta(days=rng.randint(0, span))


def _rand_datetime(rng: random.Random, d: date | None = None) -> datetime:
    d = d or _rand_date(rng)
    return datetime(d.year, d.month, d.day, rng.randint(0, 23), rng.randint(0, 59))


# ── background-row builders, one per source ────────────────────────────────


def _bg_expense_report(rng: random.Random, i: int) -> dict:
    eid, ename = rng.choice(NON_EXCO)
    return {
        "Employee": ename,
        "Employee ID": eid,
        "Employee ID(Cross Change Approver)": None,
        "Report Legacy Key": 800_000 + i,
        "Report Name": f"BG Report {800_000 + i}",
        "Transaction Date": _rand_date(rng),
        "Expense Type": rng.choice(FILLER_EXPENSE_TYPES),
        "Vendor": rng.choice(FILLER_VENDORS),
        "Parent Key": 900_000 + i,
        "Payment Type": rng.choice(["Out of Pocket", "Amex IBCP", "ANZ Visa CBCP"]),
        "Reimbursement Currency": "AUD",
        "Expense Amount (reimbursement currency)": round(rng.uniform(15, 350), 2),
        "Transaction Currency": "AUD",
        "City/Location": rng.choice(FILLER_CITIES),
    }


def _bg_attendee_validity(rng: random.Random, i: int) -> dict:
    eid, ename = rng.choice(NON_EXCO)
    return {
        "Employee ID": eid,
        "Report Name": f"BG Report {800_000 + i}",
        "Attendee Name": rng.choice(NON_EXCO)[1],
        "Expense Type": "Meals - Domestic Travel",
        "Vendor": rng.choice(FILLER_VENDORS),
        "Transaction Date": _rand_date(rng),
        "Is Attendee also in Hierarchy?": "No",
        "Number of Attendees": 1,
        "Entry Amount": round(rng.uniform(10, 60), 2),
        "External ID": 500_000 + i,
        "Company": None,
    }


def _bg_missing_receipt(rng: random.Random, i: int) -> dict:
    eid, ename = rng.choice(NON_EXCO)
    return {
        "Employee ID": eid,
        "Report Legacy Key": 810_000 + i,
        "Transaction Date": _rand_date(rng),
        "Expense Type": rng.choice(FILLER_EXPENSE_TYPES),
        "Expense Amount (reimbursement currency)": round(rng.uniform(15, 350), 2),
        "Reimbursement Currency": "AUD",
        "Has Affidavit": rng.choice(["Yes", "No"]),
    }


def _bg_approval_aging(rng: random.Random, i: int) -> dict:
    aid, aname = rng.choice(NON_EXCO)
    received = _rand_datetime(rng)
    return {
        "Report ID": f"BG-RPT-{i}",
        "Approver ID": aid,
        "Approver Name": aname,
        "Step": "1",
        "Approved Date/Time": received + timedelta(minutes=rng.randint(30, 500)),
        "Approver Received Date": received,
        "Report Receipt Viewed": rng.choice(["Yes", "No"]),
        "All Entry Receipts Viewed": rng.choice(["Yes", "No"]),
        "Minutes of Approval from Receipt View": rng.randint(5, 500),
        "Receipts Viewed Date": received,
        "Reimbursement Currency": "AUD",
    }


def _bg_travel_requests_no_expense(rng: random.Random, i: int) -> dict:
    _, ename = rng.choice(NON_EXCO)
    return {
        "Employee": ename,
        "Travel Request ID": f"BG-TRQ-{i}",
        "Approval Status": rng.choice(["Draft", "Submitted", "Sent Back to Employee"]),
        "Start Date": _rand_date(rng),
        "Total Approved Amount (rpt)": round(rng.uniform(100, 3000), 2),
    }


def _bg_travel_request_segment(rng: random.Random, i: int) -> dict:
    _, ename = rng.choice(NON_EXCO)
    return {
        "Employee": ename,
        "Expense Type": rng.choice(
            ["Airfares - Domestic Travel", "Accommodation - Domestic Travel", "Car Rentals - Domestic Travel"]
        ),
    }


def _bg_booking_detail(rng: random.Random, i: int) -> dict:
    _, ename = rng.choice(NON_EXCO)
    return {
        "Lead Traveller Name": ename,
        "Booking ID": 700_000 + i,
        "Dom | Int": rng.choice(["Domestic", "International", "Trans Tasman"]),
        "Advance Purchase Days": rng.randint(1, 60),
        "Booking Status": rng.choice(["Booked", "Ticketed", "Completed"]),
        "Depart Date": _rand_date(rng),
        "Currency": "AUD",
    }


_BACKGROUND_BUILDERS: dict[str, Callable[[random.Random, int], dict]] = {
    "expense_report": _bg_expense_report,
    "attendee_validity": _bg_attendee_validity,
    "missing_receipt": _bg_missing_receipt,
    "approval_aging": _bg_approval_aging,
    "travel_requests_no_expense": _bg_travel_requests_no_expense,
    "travel_request_segment": _bg_travel_request_segment,
    "booking_detail": _bg_booking_detail,
}

_LINE_MARKER_SOURCES = {"expense_report", "attendee_validity"}


def _contract_columns(source: str) -> list[str]:
    return list(_CONTRACT[source]["columns"].keys())


def _materialise(source: str, base: dict, fields: dict | None) -> dict:
    row = dict(base)
    if fields:
        row.update(fields)
    return row


def _row_identity(row: dict) -> tuple:
    return tuple(sorted(row.items()))


def _collect_rows(plants_doc: dict, source: str) -> list[dict]:
    """Every plant/negative `fields` dict (or group `members[*]`) declared for
    `source`, across every test in plants.yaml -- in the order first declared,
    so a re-run of generate() is deterministic given the same plants.yaml.
    Content-deduplicated: some sources (e.g. booking_detail) are deliberately
    scored by more than one test from the SAME underlying population
    (T3.1b and T3.3a_dom/_int/_very_late all read t31b_bookings_pop), so the
    identical row is declared once per test block it applies to -- it must be
    materialised once, not once per test that references it."""
    rows: list[dict] = []
    seen: set[tuple] = set()
    for test in plants_doc.get("tests", {}).values():
        if test.get("source") != source:
            continue
        for entry in test.get("plants", []) + test.get("negatives", []):
            candidates = entry["members"] if "members" in entry else [entry["fields"]]
            for row in candidates:
                key = _row_identity(row)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
    return rows


def generate(plants_path: str | Path, out_dir: str | Path) -> dict[str, int]:
    plants_path = Path(plants_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    doc = yaml.safe_load(plants_path.read_text())
    seed = doc.get("seed", DEFAULT_SEED)
    background_counts = doc.get("background", {})

    row_counts: dict[str, int] = {}

    for source, builder in _BACKGROUND_BUILDERS.items():
        rng = _rng(seed + hash(source) % 10_000)
        columns = _contract_columns(source)
        n_bg = background_counts.get(source, 0)

        rows = [builder(rng, i) for i in range(n_bg)]
        rows.extend(_collect_rows(doc, source))

        if source in _LINE_MARKER_SOURCES:
            for i, row in enumerate(rows):
                row.setdefault("Line Marker", i + 1)
            out_columns = columns + ["Line Marker"]
        else:
            out_columns = columns

        frame_rows = []
        for row in rows:
            record = {col: row.get(col) for col in out_columns}
            frame_rows.append(record)
        df = pd.DataFrame(frame_rows, columns=out_columns)

        cfg = _CONTRACT[source]
        out_path = out_dir / cfg["file"]
        df.to_excel(out_path, sheet_name=cfg.get("sheet", "Sheet1"), index=False, engine="xlsxwriter")
        row_counts[source] = len(df)

    # per_diem_rates: copied verbatim (task item 1) -- never planted.
    per_diem_cfg = _CONTRACT["per_diem_rates"]
    src_path = SYNTHETIC_DATA_DIR / per_diem_cfg["file"]
    dst_path = out_dir / per_diem_cfg["file"]
    dst_path.write_bytes(src_path.read_bytes())
    row_counts["per_diem_rates"] = sum(1 for _ in src_path.read_text().splitlines()) - 1

    return row_counts


if __name__ == "__main__":
    out = _THIS_DIR / "data"
    counts = generate(_THIS_DIR / "plants.yaml", out)
    for name, n in counts.items():
        print(f"{name}: {n} rows -> {out}")
