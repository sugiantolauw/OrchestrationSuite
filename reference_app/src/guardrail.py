"""Guardrail — Post-Generation Number Verification.

After the LLM generates findings, this module:
1. Extracts every number from the LLM's observation text
2. Cross-references each number against the evidence payload
3. Reports which numbers are supported vs unsupported
4. Each verified number gets a citation back to its metric_id and source file

The guardrail checks that a quantity is supported, not that it is
used with the right sense.
"""

import re
import logging
from typing import Any, Dict, List, Set, Tuple

logger = logging.getLogger(__name__)

DOLLAR_TOLERANCE = 1.0    # ±$1 for rounding
PCT_TOLERANCE = 0.2       # ±0.2% for rounding
COUNT_TOLERANCE = 0       # Exact match for counts


def flatten_payload_numbers(payload: Dict[str, Any]) -> Dict[float, List[str]]:
    """Recursively extract all numeric values from the payload.

    Returns a dict mapping each numeric value to the list of
    metric_id paths where it appears.
    """
    numbers: Dict[float, List[str]] = {}

    def _extract(obj, path=""):
        if isinstance(obj, (int, float)):
            val = float(obj)
            numbers.setdefault(val, []).append(path)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                _extract(v, f"{path}.{k}" if path else k)
        elif isinstance(obj, (list, tuple)):
            for i, item in enumerate(obj):
                _extract(item, f"{path}[{i}]")

    _extract(payload)
    return numbers


def extract_dollar_amounts(text: str) -> List[Tuple[str, float]]:
    """Extract all dollar amounts from text."""
    pattern = r'\$(\d[\d,]*\.?\d*)'
    matches = []
    for m in re.finditer(pattern, text):
        raw = m.group(1).replace(',', '')
        try:
            matches.append((m.group(0), float(raw)))
        except ValueError:
            continue
    return matches


def extract_percentages(text: str) -> List[Tuple[str, float]]:
    """Extract all percentages from text."""
    pattern = r'(\d+\.?\d*)\s*%'
    matches = []
    for m in re.finditer(pattern, text):
        try:
            matches.append((f"{m.group(1)}%", float(m.group(1))))
        except ValueError:
            continue
    return matches


def extract_counts(text: str) -> List[Tuple[str, float]]:
    """Extract counts followed by domain keywords."""
    pattern = (
        r'(\d[\d,]*)\s+(?:of\s+\d[\d,]*\s+)?'
        r'(?:claims?|reports?|members?|exceptions?|bookings?|days?|files?|'
        r'records?|months?|rows?|pairs?|pre-?approvals?|employees?|'
        r'approvers?|events?|items?|entries?|combinations?|duplicates?)'
    )
    matches = []
    for m in re.finditer(pattern, text, re.IGNORECASE):
        raw = m.group(1).replace(',', '')
        try:
            matches.append((m.group(0), float(raw)))
        except ValueError:
            continue
    return matches


def find_match(value: float, number_map: Dict[float, List[str]],
               tolerance: float) -> Tuple[bool, List[str]]:
    """Check if a value matches any number in the payload within tolerance.

    Returns (matched, list_of_metric_paths).
    """
    for candidate, paths in number_map.items():
        if abs(value - candidate) <= tolerance:
            return True, paths
    return False, []


def verify_finding(finding: Dict[str, Any],
                   payload: Dict[str, Any]) -> Dict[str, Any]:
    """Verify all numbers in a single finding's observation against the payload.

    Returns a dict with:
      - passed: bool
      - checks: list of {text, value, kind, matched, metric_paths, source_files}
      - stats: {total, verified, unsupported}
    """
    observation = finding.get("observation", "")
    number_map = flatten_payload_numbers(payload)

    # Also include the metric source_file mapping
    source_map = {}  # metric_id -> source_file
    for key, val in payload.get("metrics", {}).items():
        if isinstance(val, dict) and "source_file" in val:
            source_map[key] = val["source_file"]

    checks = []

    for display, value in extract_dollar_amounts(observation):
        matched, paths = find_match(value, number_map, DOLLAR_TOLERANCE)
        source_files = _resolve_sources(paths, source_map)
        checks.append({
            "text": display, "value": value, "kind": "dollar",
            "matched": matched, "metric_paths": paths, "source_files": source_files,
        })

    for display, value in extract_percentages(observation):
        matched, paths = find_match(value, number_map, PCT_TOLERANCE)
        source_files = _resolve_sources(paths, source_map)
        checks.append({
            "text": display, "value": value, "kind": "percentage",
            "matched": matched, "metric_paths": paths, "source_files": source_files,
        })

    for display, value in extract_counts(observation):
        matched, paths = find_match(value, number_map, COUNT_TOLERANCE)
        source_files = _resolve_sources(paths, source_map)
        checks.append({
            "text": display, "value": value, "kind": "count",
            "matched": matched, "metric_paths": paths, "source_files": source_files,
        })

    verified = [c for c in checks if c["matched"]]
    unsupported = [c for c in checks if not c["matched"]]

    result = {
        "passed": len(unsupported) == 0,
        "checks": checks,
        "stats": {
            "total": len(checks),
            "verified": len(verified),
            "unsupported": len(unsupported),
        },
    }

    if unsupported:
        logger.warning(
            "Finding '%s' guardrail: %d/%d unsupported — %s",
            finding.get("test_id", "?"),
            len(unsupported), len(checks),
            [u["text"] for u in unsupported],
        )
    else:
        logger.info(
            "Finding '%s' guardrail: %d/%d verified ✓",
            finding.get("test_id", "?"),
            len(verified), len(checks),
        )

    return result


def verify_all_findings(findings: list,
                        payload: Dict[str, Any]) -> Dict[str, Any]:
    """Verify all findings against the evidence payload.

    Returns:
      - per_finding: dict of test_id -> verification result
      - summary: {total_checks, total_verified, total_unsupported, all_passed}
    """
    per_finding = {}
    total_checks = 0
    total_verified = 0
    total_unsupported = 0

    for f in findings:
        fid = f.get("test_id", f.get("finding_id", "unknown"))
        result = verify_finding(f, payload)
        per_finding[fid] = result
        total_checks += result["stats"]["total"]
        total_verified += result["stats"]["verified"]
        total_unsupported += result["stats"]["unsupported"]

    return {
        "per_finding": per_finding,
        "summary": {
            "total_checks": total_checks,
            "total_verified": total_verified,
            "total_unsupported": total_unsupported,
            "all_passed": total_unsupported == 0,
        },
    }


def _resolve_sources(paths: List[str], source_map: Dict[str, str]) -> List[str]:
    """Given metric paths, resolve to source file names."""
    files = set()
    for path in paths:
        # path looks like "metrics.approver_no_receipt_pct.value"
        # extract the metric_id (second segment)
        parts = path.split(".")
        if len(parts) >= 2:
            metric_id = parts[1] if parts[0] == "metrics" else parts[0]
            if metric_id in source_map:
                files.add(source_map[metric_id])
        # Also check if any parent key matches a source
        for key in source_map:
            if key in path:
                files.add(source_map[key])
    return sorted(files)
