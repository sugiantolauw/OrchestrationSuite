"""Data Loader — Load all 8 audit data files from Unity Catalog Volume.

Reads from /Volumes/dvlp_11/ia_dart/tne_exco/ and returns pandas DataFrames.
Handles Excel sheets, CSV, and validates row counts on load.
"""

import os
import logging
from difflib import SequenceMatcher
from typing import Dict

import pandas as pd

logger = logging.getLogger(__name__)

# Default data volume path — overridable via env var
DATA_VOLUME_PATH = os.environ.get("DATA_VOLUME_PATH", "/dbfs/Volumes/dvlp_11/ia_dart/tne_exco/")

# File definitions: key -> (filename, sheet_name or None for CSV, expected_rows)
FILE_REGISTRY = {
    "expense_report": {
        "filename": "Optus expense report Combined - 01 Jan 25 - 30 Apr 2026.xlsx",
        "pattern": "optus expense report combined",
        "sheet": "data",
        "expected_rows": 92_798,
    },
    "attendee_validity": {
        "filename": "Attendee Validity Report_010125_300426.xlsx",
        "pattern": "attendee validity report",
        "sheet": None,  # first sheet
        "expected_rows": 1_394,
    },
    "missing_receipt": {
        "filename": "Claims with Missing Receipt Affidavit_010125_300426.xlsx",
        "pattern": "claims with missing receipt affidavit",
        "sheet": None,
        "expected_rows": 871,
    },
    "approval_aging": {
        "filename": "Approval Aging Details - Receipt Viewed_010125_010426.xlsx",
        "pattern": "approval aging details",
        "sheet": None,
        "expected_rows": 29_103,
    },
    "travel_requests_no_expense": {
        "filename": "Authorised Travel Requests Without Expense Report Entry_010125_300426.xlsx",
        "pattern": "authorised travel requests without expense report entry",
        "sheet": None,
        "expected_rows": 4_784,
    },
    "travel_request_segment": {
        "filename": "Travel Request by Segment_010125_200426.xlsx",
        "pattern": "travel request by segment",
        "sheet": None,
        "expected_rows": 16_747,
    },
    "booking_detail": {
        "filename": "Booking Detail Report_010125_300426.xlsx",
        "pattern": "booking detail report",
        "sheet": None,
        "expected_rows": 7_019,
    },
    "per_diem_rates": {
        "filename": "Complete_Per_Diem_Rates_by_Country_Region.csv",
        "pattern": "complete_per_diem_rates_by_country_region",
        "sheet": None,
        "expected_rows": 205,
    },
}


def _resolve_volume_file(volume_path: str, expected_filename: str, pattern: str = "") -> str:
    """Resolve a file path in volume with tolerant matching.

    Tries exact filename first; if missing, falls back to pattern and
    similarity-based matching inside the same directory.
    """
    exact_path = os.path.join(volume_path, expected_filename)
    if os.path.exists(exact_path):
        return exact_path

    try:
        all_files = [f for f in os.listdir(volume_path) if os.path.isfile(os.path.join(volume_path, f))]
    except Exception as e:
        raise FileNotFoundError(
            f"Could not list files in {volume_path} while resolving {expected_filename}: {e}"
        ) from e

    expected_ext = os.path.splitext(expected_filename)[1].lower()
    expected_base = os.path.splitext(expected_filename)[0].lower()
    pattern_lc = (pattern or "").lower()

    ext_candidates = [f for f in all_files if os.path.splitext(f)[1].lower() == expected_ext]
    if not ext_candidates:
        ext_candidates = all_files

    scored = []
    for filename in ext_candidates:
        name_lc = filename.lower()
        stem_lc = os.path.splitext(name_lc)[0]
        score = SequenceMatcher(None, expected_base, stem_lc).ratio()

        if pattern_lc and pattern_lc in name_lc:
            score += 0.35

        scored.append((score, filename))

    scored.sort(key=lambda x: x[0], reverse=True)

    if not scored or scored[0][0] < 0.45:
        sample = ", ".join(sorted(all_files)[:12])
        raise FileNotFoundError(
            f"Could not resolve file for expected '{expected_filename}' in '{volume_path}'. "
            f"Top files found: {sample}"
        )

    best_match = scored[0][1]
    resolved_path = os.path.join(volume_path, best_match)
    logger.warning(
        "Resolved missing expected file '%s' to '%s'",
        expected_filename,
        best_match,
    )
    return resolved_path


def _candidate_volume_paths(volume_path: str):
    """Return candidate filesystem paths for Databricks Volumes.

    Databricks Apps runtimes often require `/dbfs/Volumes/...` while
    notebooks may expose `/Volumes/...` directly.
    """
    raw = (volume_path or "").strip()
    if not raw:
        return []

    candidates = []

    def _add(p):
        p = p.rstrip("/") + "/"
        if p not in candidates:
            candidates.append(p)

    _add(raw)

    if raw.startswith("dbfs:/"):
        _add("/dbfs/" + raw[len("dbfs:/"):])

    if raw.startswith("/Volumes/"):
        _add("/dbfs" + raw)

    if raw.startswith("/dbfs/Volumes/"):
        _add(raw[len("/dbfs"):])

    return candidates


def load_file(key: str, volume_path: str = None) -> pd.DataFrame:
    """Load a single data file by registry key.

    Args:
        key: Registry key (e.g., 'expense_report', 'booking_detail')
        volume_path: Override path to data volume

    Returns:
        pandas DataFrame with loaded data
    """
    if volume_path is None:
        volume_path = DATA_VOLUME_PATH

    config = FILE_REGISTRY[key]
    filepath = None
    tried_paths = []
    last_error = None

    for candidate_volume_path in _candidate_volume_paths(volume_path):
        tried_paths.append(candidate_volume_path)
        try:
            filepath = _resolve_volume_file(
                candidate_volume_path,
                config["filename"],
                config.get("pattern", ""),
            )
            break
        except Exception as e:
            last_error = e

    if filepath is None:
        tried = ", ".join(tried_paths)
        raise FileNotFoundError(
            f"Unable to resolve '{config['filename']}' for key '{key}'. "
            f"Tried volume paths: {tried}. Last error: {last_error}"
        )

    logger.info(f"Loading {key} from {filepath}")

    if filepath.endswith(".csv"):
        df = pd.read_csv(filepath)
    else:
        sheet = config["sheet"] if config["sheet"] else 0
        df = pd.read_excel(filepath, sheet_name=sheet, engine="openpyxl")

    actual_rows = len(df)
    expected_rows = config["expected_rows"]

    if actual_rows != expected_rows:
        logger.warning(
            f"{key}: Expected {expected_rows:,} rows, got {actual_rows:,} rows. "
            f"Delta: {actual_rows - expected_rows:+,}"
        )
    else:
        logger.info(f"{key}: Loaded {actual_rows:,} rows ✓")

    return df


def load_all_files(volume_path: str = None) -> Dict[str, pd.DataFrame]:
    """Load all 8 data files and return as a dictionary.

    Args:
        volume_path: Override path to data volume

    Returns:
        Dict mapping registry keys to DataFrames
    """
    data = {}
    for key in FILE_REGISTRY:
        try:
            data[key] = load_file(key, volume_path)
        except FileNotFoundError as e:
            logger.error(f"File not found for {key}: {e}")
            raise
        except Exception as e:
            logger.error(f"Error loading {key}: {e}")
            raise

    logger.info(f"All {len(data)} files loaded successfully.")
    return data


# ExCo member list — authoritative source
EXCO_MEMBERS = [
    "Giles Knopp, Andy",
    "Shiner, Anthony",
    "Ivanoff, Betty",
    "Ross, Felicity",
    "Berejiklian, Gladys",
    "McInerney, John",
    "Aitken, Kate Alice",
    "Dyer, Kathrine",
    "van der Merwe, Pieter",
    "Sriharan, Sri",
    "Butler, Viktoriya Grant Crichton",
]


def build_exco_populations(df_expense: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """Derive the three ExCo populations from the primary expense report.

    Args:
        df_expense: Primary expense report DataFrame (File 1)

    Returns:
        Dict with keys: 'prepared', 'approved', 'combined'
    """
    # ExCo Prepared: claims prepared BY an ExCo member
    df_prepared = df_expense[df_expense["Employee"].isin(EXCO_MEMBERS)].copy()
    df_prepared["Source_Population"] = "Prepared"

    # ExCo Approved: claims approved BY an ExCo member
    df_approved = df_expense[
        df_expense["Cross Charge Approver"].isin(EXCO_MEMBERS)
    ].copy()
    df_approved["Source_Population"] = "Approved"

    # Combined (primary testing population)
    df_combined = pd.concat([df_prepared, df_approved], ignore_index=True)

    populations = {
        "prepared": df_prepared,
        "approved": df_approved,
        "combined": df_combined,
    }

    logger.info(
        f"Populations built — Prepared: {len(df_prepared):,}, "
        f"Approved: {len(df_approved):,}, Combined: {len(df_combined):,}"
    )

    return populations
