"""orchestrator.explorer.materialise + orchestrator.skills.
load_skill_from_ledger (CLAUDE.md §8 P8 DoD gate test item 6). Fully
offline except the deliberate real-subprocess G9-style check."""

from __future__ import annotations

import json
import os
import string
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from orchestrator.explorer.canonical import to_canonical
from orchestrator.explorer.materialise import check_materialised_skill, materialise
from orchestrator.fingerprint import hash_skill_content_entries
from orchestrator.skills import SkillValidationError, load_skill, load_skill_from_ledger

REPO_ROOT = Path(__file__).resolve().parent.parent


def _wire() -> dict:
    return {
        "schema_version": "explorer-plan/1", "skill_name": "Test Skill", "domain": "Travel",
        "summary": "A summary with no numbers",
        "sources": [{"source": "expense_report", "amount_column": "Amount", "date_column": "Transaction Date",
                     "entry_key": ["Employee ID"]}],
        "populations": [{"key": "p1", "source": "expense_report", "description": "All claims",
                          "filters": [{"column": "Category", "op": "eq", "value": "Travel"}]}],
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
                "metrics": [
                    {"name": "hv_count", "kind": "count", "column": None, "key": None, "unit": "count", "where": None},
                    {"name": "hv_amount", "kind": "sum", "column": "Amount", "key": None, "unit": "currency", "where": None},
                ],
            },
            "control_key": "c1", "risk_key": "r1", "assertion": "operating",
            "control_objective": "objective text", "risk_hypothesis": "hypothesis text",
            "rationale": "rationale text",
        }],
        "findings": [{
            "key": "f1", "test_key": "t1", "title": "High value claims", "trigger": "hv_count > 0",
            "severity": [{"when": "hv_count > 0", "then": "High"}, {"when": None, "then": "Low"}],
            "metrics_cited": ["hv_count", "hv_amount"], "thresholds_cited": ["hv"], "monetary_basis": "spend",
            "observation": "{hv_count} claims over the limit, totalling {hv_amount}.",
            "recommendation": "Review high value claims.",
            "management_questions": ["What controls exist?"],
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
        {"name": "Category", "type": "string", "null_count": 0, "distinct_count": 2, "unique": False,
         "semantic_type": "category", "pii": False, "pii_basis": None,
         "values": [{"value": "Travel", "count": 6}, {"value": "Meals", "count": 4}], "suppressed_values": 0},
        {"name": "Currency", "type": "string", "null_count": 0, "distinct_count": 1, "unique": False,
         "semantic_type": "currency_code", "pii": False, "pii_basis": None,
         "values": [{"value": "AUD", "count": 10}], "suppressed_values": 0},
        {"name": "Employee ID", "type": "integer", "null_count": 0, "distinct_count": 10, "unique": True,
         "semantic_type": "identifier", "pii": True, "pii_basis": "heuristic"},
    ]}}


def _run_sources() -> list[dict]:
    return [{"name": "expense_report", "kind": "local_file", "format": "csv", "file": "expense_report.csv"}]


def _materialise():
    effective = to_canonical(_wire())
    return materialise(
        effective, profile=_profile(), run_sources=_run_sources(), run_id="RUN1",
        confirmed_at="2026-09-24T00:00:00Z", owner="alice", audit_timezone="Australia/Sydney",
    )


def _write(files: dict[str, bytes], tmp_path: Path) -> Path:
    for name, content in files.items():
        (tmp_path / name).write_bytes(content)
    return tmp_path


# ── the materialised Skill passes load_skill + validate_skill ──────────────


def test_materialised_skill_loads_and_validates(tmp_path):
    files = _materialise()
    skill = load_skill(_write(files, tmp_path))
    skill.validate()
    assert skill.skill_id == "EXPLORER-RUN1"
    assert skill.version == "run"
    assert skill.manifest["status"] == "draft"


def test_materialised_skill_contains_no_python_files(tmp_path):
    files = _materialise()
    assert all(not name.endswith(".py") for name in files)


# ── thresholds are analyst-set / pending_policy_confirmation ───────────────


def test_materialised_thresholds_are_analyst_set(tmp_path):
    files = _materialise()
    thresholds = yaml.safe_load(files["thresholds.yaml"])
    assert thresholds
    for tid, spec in thresholds.items():
        assert spec["provenance"]["type"] == "analyst-set"
        assert spec["provenance"]["pending_policy_confirmation"] is True


def test_materialised_currency_unit_resolved_from_placeholder(tmp_path):
    files = _materialise()
    thresholds = yaml.safe_load(files["thresholds.yaml"])
    assert thresholds["hv"]["unit"] == "AUD"
    plan = yaml.safe_load(files["plan.yaml"])
    metrics = plan["tests"][0]["params"]["metrics"]
    assert metrics["hv_amount"]["unit"] == "AUD"


# ── G8-shaped: catalogue.yaml threshold text renders against thresholds.yaml ──


def test_catalogue_threshold_text_renders_like_g8_expects(tmp_path):
    files = _materialise()
    catalogue = yaml.safe_load(files["catalogue.yaml"])
    thresholds = yaml.safe_load(files["thresholds.yaml"])
    thr_values = {tid: spec["value"] for tid, spec in thresholds.items()}
    fmt = string.Formatter()
    for entry in catalogue["tests"]:
        placeholders = {fn for _, fn, _, _ in fmt.parse(entry["threshold"]) if fn}
        assert placeholders <= set(thr_values)
        entry["threshold"].format(**thr_values)


# ── V-Z1: the belt-and-braces check ─────────────────────────────────────


def test_check_materialised_skill_passes_for_a_valid_proposal():
    effective = to_canonical(_wire())
    result = check_materialised_skill(
        effective, profile=_profile(), run_sources=_run_sources(), run_id="RUN1",
        confirmed_at="2026-09-24T00:00:00Z", owner="alice", audit_timezone="Australia/Sydney",
    )
    assert result == {"proposal_errors": [], "test_violations": {}}


# ── content_hash is stable across two subprocesses (G9-shaped) ─────────


def test_materialise_content_hash_stable_across_two_subprocesses(tmp_path):
    worker = REPO_ROOT / "tests" / "g9_materialise_subprocess_worker.py"
    results = []
    for i, hash_seed in enumerate(("1", "2"), start=1):
        out_path = tmp_path / f"g9-materialise-{i}.json"
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        proc = subprocess.run(
            [sys.executable, str(worker), str(out_path)],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, f"worker {i} (PYTHONHASHSEED={hash_seed}) failed:\n{proc.stdout}\n{proc.stderr}"
        results.append(json.loads(out_path.read_text()))

    assert results[0]["content_hash"] == results[1]["content_hash"]
    assert results[0]["files"] == results[1]["files"]


# ── load_skill_from_ledger ───────────────────────────────────────────────


def _ledger_row(files: dict[str, bytes]) -> dict:
    entries = list(files.items())
    content_hash = hash_skill_content_entries(entries)
    return {
        "skill_id": "EXPLORER-RUN1", "content_hash": content_hash,
        "content": {"files": {name: content.decode("utf-8") for name, content in files.items()}},
    }


def test_load_skill_from_ledger_round_trips():
    files = _materialise()
    row = _ledger_row(files)
    skill = load_skill_from_ledger(row)
    assert skill.skill_id == "EXPLORER-RUN1"
    assert skill.content_hash == row["content_hash"]
    skill.validate()


def test_load_skill_from_ledger_is_idempotent_across_calls():
    files = _materialise()
    row = _ledger_row(files)
    skill1 = load_skill_from_ledger(row)
    skill2 = load_skill_from_ledger(row)
    assert skill1.skill_dir == skill2.skill_dir
    assert skill1.content_hash == skill2.content_hash


def test_load_skill_from_ledger_rejects_a_py_entry():
    files = _materialise()
    row = _ledger_row(files)
    row["content"]["files"]["custom.py"] = "print('hello')"
    with pytest.raises(SkillValidationError, match="custom.py"):
        load_skill_from_ledger(row)


def test_load_skill_from_ledger_rejects_an_unexpected_path():
    files = _materialise()
    row = _ledger_row(files)
    row["content"]["files"]["../escape.yaml"] = "a: 1"
    with pytest.raises(SkillValidationError):
        load_skill_from_ledger(row)


def test_load_skill_from_ledger_detects_hash_tampering():
    files = _materialise()
    row = _ledger_row(files)
    tampered_hash = "0" * 64
    row = {**row, "content_hash": tampered_hash}
    # Force a fresh cache dir (a different content_hash) so this does not
    # collide with a prior test's cached directory for the real hash.
    with pytest.raises(SkillValidationError, match="content_hash mismatch"):
        load_skill_from_ledger(row)
