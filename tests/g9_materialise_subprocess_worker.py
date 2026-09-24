"""Standalone worker for materialise()'s own G9-style cross-process
determinism check (docs/specs/P6_P8_explorer_llm_design.md §8 item 6: "
content_hash is stable across two subprocesses"). Run as a real subprocess
(never imported by pytest) so PYTHONHASHSEED genuinely differs between the
two invocations, the same reasoning tests/g9_subprocess_worker.py's own
docstring gives for the fieldwork pipeline's G9 gate.

Usage: python tests/g9_materialise_subprocess_worker.py <output_json_path>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.explorer.canonical import to_canonical  # noqa: E402
from orchestrator.explorer.materialise import materialise  # noqa: E402
from orchestrator.fingerprint import hash_skill_content_entries  # noqa: E402


def _fixture_wire() -> dict:
    return {
        "schema_version": "explorer-plan/1", "skill_name": "Test Skill", "domain": "Travel",
        "summary": "A summary with no numbers",
        "sources": [{"source": "expense_report", "amount_column": "Amount", "date_column": "Transaction Date",
                     "entry_key": ["Employee ID"]}],
        "populations": [{"key": "p1", "source": "expense_report", "description": "All claims", "filters": []}],
        "risks": [{"key": "r1", "title": "A risk", "description": "Some risk"}],
        "controls": [{"key": "c1", "risk_key": "r1", "title": "A control", "description": "Some control",
                      "type": "preventive"}],
        "thresholds": [{"id": "hv", "value": 50, "unit": "currency", "description": "High value limit"}],
        "tests": [{
            "key": "t1", "name": "High value test", "primitive": "threshold_exceedance",
            "params": {
                "kind": "threshold_exceedance", "population": "p1", "column": "Amount",
                "limit": {"threshold": "hv"}, "direction": "above",
                "group_by": None, "aggregate": None, "exclude": None,
                "metrics": [{"name": "hv_count", "kind": "count", "column": None, "key": None,
                             "unit": "count", "where": None}],
            },
            "control_key": "c1", "risk_key": "r1", "assertion": "operating",
            "control_objective": "objective text", "risk_hypothesis": "hypothesis text",
            "rationale": "rationale text",
        }],
        "findings": [{
            "key": "f1", "test_key": "t1", "title": "High value claims", "trigger": "hv_count > 0",
            "severity": [{"when": None, "then": "Low"}],
            "metrics_cited": ["hv_count"], "thresholds_cited": [], "monetary_basis": "none",
            "observation": "{hv_count} claims over the limit.", "recommendation": "Review high value claims.",
            "management_questions": [],
        }],
        "data_gaps": [], "assumptions": [],
    }


def _profile() -> dict:
    return {"expense_report": {"row_count": 10, "null_counts": {}, "columns": [
        {"name": "Amount", "type": "number", "null_count": 0, "distinct_count": 10, "unique": False,
         "semantic_type": "amount", "pii": False, "pii_basis": None, "min": 1.0, "max": 100.0,
         "negative_count": 0, "zero_count": 0},
        {"name": "Transaction Date", "type": "date", "null_count": 0, "distinct_count": 5, "unique": False,
         "semantic_type": "date", "pii": False, "pii_basis": None, "min": "2026-01-01", "max": "2026-01-31"},
        {"name": "Currency", "type": "string", "null_count": 0, "distinct_count": 1, "unique": False,
         "semantic_type": "currency_code", "pii": False, "pii_basis": None,
         "values": [{"value": "AUD", "count": 10}], "suppressed_values": 0},
        {"name": "Employee ID", "type": "integer", "null_count": 0, "distinct_count": 10, "unique": True,
         "semantic_type": "identifier", "pii": True, "pii_basis": "heuristic"},
    ]}}


def main() -> None:
    output_path = sys.argv[1]
    effective = to_canonical(_fixture_wire())
    run_sources = [{"name": "expense_report", "kind": "local_file", "format": "csv", "file": "expense_report.csv"}]
    files = materialise(
        effective, profile=_profile(), run_sources=run_sources, run_id="G9RUN",
        confirmed_at="2026-09-24T00:00:00Z", owner="gatebot", audit_timezone="Australia/Sydney",
    )
    entries = list(files.items())
    content_hash = hash_skill_content_entries(entries)
    Path(output_path).write_text(json.dumps({
        "content_hash": content_hash,
        "files": {name: content.decode("utf-8") for name, content in files.items()},
    }, sort_keys=True))


if __name__ == "__main__":
    main()
