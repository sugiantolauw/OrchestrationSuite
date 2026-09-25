"""docs/specs/P7_mapping_authoring_design.md §2.4: one broken copy of
tests/fixtures/skills/mini per check, each producing exactly its named
error; collect-all reports errors from two files at once; custom.py ->
warning; CLI exit codes and --json."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml

from orchestrator.authoring.__main__ import main as authoring_main
from orchestrator.authoring.checks import validate_skill_dir

MINI = Path(__file__).parent / "fixtures" / "skills" / "mini"


def _copy_mini(tmp_path: Path) -> Path:
    dest = tmp_path / "mini"
    shutil.copytree(MINI, dest)
    return dest


def _find(report, name: str) -> dict:
    return next(c for c in report.checks if c["name"] == name)


def test_a_clean_copy_of_mini_passes():
    report = validate_skill_dir(MINI)
    assert report.ok, report.to_dict()


def test_missing_required_file_fails_required_files_check(tmp_path: Path):
    dest = _copy_mini(tmp_path)
    (dest / "findings.yaml").unlink()
    report = validate_skill_dir(dest)
    assert not report.ok
    check = _find(report, "required files and schemas")
    assert not check["passed"]
    assert "missing required Skill file: findings.yaml" in check["detail"]


def test_schema_violation_fails_required_files_check(tmp_path: Path):
    dest = _copy_mini(tmp_path)
    manifest = yaml.safe_load((dest / "manifest.yaml").read_text())
    del manifest["status"]
    (dest / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    report = validate_skill_dir(dest)
    assert not report.ok
    check = _find(report, "required files and schemas")
    assert not check["passed"]
    assert "manifest.yaml" in check["detail"]


def test_unknown_threshold_reference_fails_semantic_rules(tmp_path: Path):
    dest = _copy_mini(tmp_path)
    thresholds = yaml.safe_load((dest / "thresholds.yaml").read_text())
    del thresholds["hv_limit"]
    (dest / "thresholds.yaml").write_text(yaml.safe_dump(thresholds))
    report = validate_skill_dir(dest)
    assert not report.ok
    check = _find(report, "validate_skill (semantic rules)")
    assert not check["passed"]
    assert "hv_limit" in check["detail"]


def test_unused_threshold_fails_g8_but_not_semantic_rules(tmp_path: Path):
    dest = _copy_mini(tmp_path)
    thresholds = yaml.safe_load((dest / "thresholds.yaml").read_text())
    thresholds["unused_thr"] = {
        "value": 1, "unit": "count", "description": "never referenced",
        "used_by": [], "effective_date": "2025-01-01",
        "provenance": {"type": "analyst-set", "pending_policy_confirmation": True},
    }
    (dest / "thresholds.yaml").write_text(yaml.safe_dump(thresholds))
    report = validate_skill_dir(dest)
    assert not report.ok
    assert _find(report, "validate_skill (semantic rules)")["passed"]
    g8 = _find(report, "G8 threshold provenance")
    assert not g8["passed"]
    assert "unused_thr" in g8["detail"]


def test_content_lock_mismatch_fails_content_lock_check(tmp_path: Path):
    dest = _copy_mini(tmp_path)
    lock = {"version": "0.1.0", "content_hash": "not-the-real-hash"}
    (dest / "content.lock").write_text(yaml.safe_dump(lock))
    report = validate_skill_dir(dest)
    assert not report.ok
    check = _find(report, "content.lock")
    assert not check["passed"]
    assert "content_hash is now" in check["detail"]


def test_forbidden_substring_in_prose_fails_prose_check(tmp_path: Path):
    dest = _copy_mini(tmp_path)
    findings = yaml.safe_load((dest / "findings.yaml").read_text())
    findings["findings"][0]["observation"] = "See http://example.com for details."
    (dest / "findings.yaml").write_text(yaml.safe_dump(findings))
    report = validate_skill_dir(dest)
    assert not report.ok
    check = _find(report, "prose templates (no code substrings)")
    assert not check["passed"]


def test_custom_py_present_is_a_warning_not_an_error(tmp_path: Path):
    dest = _copy_mini(tmp_path)
    (dest / "custom.py").write_text("CUSTOM_PRIMITIVES = {}\n")
    report = validate_skill_dir(dest)
    assert report.ok, report.to_dict()
    check = _find(report, "code presence (custom.py / workspace.py)")
    assert not check["passed"]
    assert "custom.py present" in check["detail"]
    assert any("custom.py present" in w for w in report.warnings)
    assert not any("custom.py present" in e for e in report.errors)


def test_collect_all_reports_violations_from_two_files_at_once(tmp_path: Path):
    dest = _copy_mini(tmp_path)
    manifest = yaml.safe_load((dest / "manifest.yaml").read_text())
    del manifest["status"]
    (dest / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    contract = yaml.safe_load((dest / "contract.yaml").read_text())
    del contract["timezone"]
    (dest / "contract.yaml").write_text(yaml.safe_dump(contract))

    report = validate_skill_dir(dest)
    check = _find(report, "required files and schemas")
    assert "manifest.yaml" in check["detail"]
    assert "contract.yaml" in check["detail"]


def test_cli_validate_exit_code_zero_on_a_clean_skill():
    assert authoring_main(["validate", str(MINI)]) == 0


def test_cli_validate_exit_code_one_on_a_broken_skill(tmp_path: Path):
    dest = _copy_mini(tmp_path)
    (dest / "findings.yaml").unlink()
    assert authoring_main(["validate", str(dest)]) == 1


def test_cli_validate_json_output(capsys):
    exit_code = authoring_main(["validate", str(MINI), "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert isinstance(payload["checks"], list)
