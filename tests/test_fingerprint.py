from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.config import Settings, load_settings
from orchestrator.errors import ConfigError, FingerprintMismatch
from orchestrator.fingerprint import compute_fingerprint, verify_fingerprint

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = REPO_ROOT / "requirements.txt"


def _settings(**overrides) -> Settings:
    base = dict(model_sonnet="m-sonnet", model_gpt_oss="m-gptoss", code_revision="fixedrev1")
    base.update(overrides)
    return Settings(**base)


def _fp(**overrides):
    kwargs = dict(
        settings=_settings(),
        source_table_versions={"catalog.schema.expense_report": "12"},
        uploaded_file_hashes={},
        skill_dir=None,
        requirements_path=REQUIREMENTS,
        prompts_dirs=[],
    )
    kwargs.update(overrides)
    return compute_fingerprint(**kwargs)


def test_deterministic_within_process():
    fp1 = _fp()
    fp2 = _fp()
    assert fp1["fingerprint_id"] == fp2["fingerprint_id"]


def test_deterministic_across_subprocess_with_different_hash_seed():
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from pathlib import Path\n"
        "from orchestrator.config import Settings\n"
        "from orchestrator.fingerprint import compute_fingerprint\n"
        "settings = Settings(model_sonnet='m-sonnet', model_gpt_oss='m-gptoss', code_revision='fixedrev1')\n"
        "fp = compute_fingerprint(settings=settings, source_table_versions={'catalog.schema.expense_report': '12'}, "
        "uploaded_file_hashes={}, skill_dir=None, requirements_path=Path(%r), prompts_dirs=[])\n"
        "print(fp['fingerprint_id'])\n"
    ) % (str(REPO_ROOT), str(REQUIREMENTS))

    ids = []
    for seed in ("1", "2"):
        env = {"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin:/usr/local/bin"}
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=str(REPO_ROOT)
        )
        assert result.returncode == 0, result.stderr
        ids.append(result.stdout.strip())
    assert ids[0] == ids[1]
    assert ids[0] == _fp()["fingerprint_id"]


def test_changes_when_source_table_version_changes():
    fp1 = _fp()
    fp2 = _fp(source_table_versions={"catalog.schema.expense_report": "13"})
    assert fp1["fingerprint_id"] != fp2["fingerprint_id"]


def test_changes_when_uploaded_file_hash_changes():
    fp1 = _fp(uploaded_file_hashes={"/Volumes/x/a.csv": "aaa"})
    fp2 = _fp(uploaded_file_hashes={"/Volumes/x/a.csv": "bbb"})
    assert fp1["fingerprint_id"] != fp2["fingerprint_id"]


def test_changes_when_skill_file_byte_changes(tmp_path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "manifest.yaml").write_text("id: SKILL-001\nversion: 1\n")
    (skill_dir / "contract.yaml").write_text("sources: []\n")
    fp1 = _fp(skill_dir=skill_dir)

    (skill_dir / "manifest.yaml").write_text("id: SKILL-001\nversion: 2\n")
    fp2 = _fp(skill_dir=skill_dir)

    assert fp1["fingerprint_id"] != fp2["fingerprint_id"]
    assert fp1["skill_content_hash"] != fp2["skill_content_hash"]


def test_skill_content_hash_none_for_explorer():
    fp = _fp(skill_dir=None)
    assert fp["skill_content_hash"] is None


def test_changes_when_requirements_change(tmp_path):
    req1 = tmp_path / "requirements1.txt"
    req1.write_text("pandas>=2.1\n")
    req2 = tmp_path / "requirements2.txt"
    req2.write_text("pandas>=2.2\n")
    fp1 = _fp(requirements_path=req1)
    fp2 = _fp(requirements_path=req2)
    assert fp1["fingerprint_id"] != fp2["fingerprint_id"]
    assert fp1["dependency_lock_hash"] != fp2["dependency_lock_hash"]


def test_changes_when_config_value_changes():
    fp1 = _fp(settings=_settings(max_concurrent_runs=2))
    fp2 = _fp(settings=_settings(max_concurrent_runs=3))
    assert fp1["fingerprint_id"] != fp2["fingerprint_id"]
    assert fp1["runtime_config_hash"] != fp2["runtime_config_hash"]


def test_changes_when_endpoint_changes():
    fp1 = _fp(settings=_settings(model_sonnet="model-a"))
    fp2 = _fp(settings=_settings(model_sonnet="model-b"))
    assert fp1["fingerprint_id"] != fp2["fingerprint_id"]
    assert fp1["endpoint_config"] != fp2["endpoint_config"]


def test_changes_when_prompt_file_changes(tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "find.md").write_text("v1")
    fp1 = _fp(prompts_dirs=[prompts])

    (prompts / "find.md").write_text("v2")
    fp2 = _fp(prompts_dirs=[prompts])

    assert fp1["fingerprint_id"] != fp2["fingerprint_id"]
    assert fp1["prompt_template_version"] != fp2["prompt_template_version"]


def test_prompt_template_version_is_explicit_none_sentinel_when_absent():
    fp = _fp(prompts_dirs=[])
    assert fp["prompt_template_version"] == "none"


def test_secrets_never_included(monkeypatch):
    monkeypatch.setenv("DATABRICKS_TOKEN", "supersecrettoken123")
    settings = load_settings({"DATABRICKS_TOKEN": "supersecrettoken123", "CODE_REVISION": "fixedrev1"})
    fp = compute_fingerprint(
        settings=settings, source_table_versions={}, uploaded_file_hashes={},
        skill_dir=None, requirements_path=REQUIREMENTS, prompts_dirs=[],
    )
    serialized = json.dumps(fp)
    assert "supersecrettoken123" not in serialized


def test_missing_code_revision_raises_config_error(tmp_path, monkeypatch):
    # An empty directory with no git repo and no explicit/settings revision must fail loudly.
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    import orchestrator.fingerprint as fp_mod

    monkeypatch.setattr(fp_mod, "_REPO_ROOT", tmp_path)
    with pytest.raises(ConfigError):
        compute_fingerprint(
            settings=settings, source_table_versions={}, uploaded_file_hashes={},
            skill_dir=None, requirements_path=REQUIREMENTS, prompts_dirs=[],
        )


def test_explicit_code_revision_wins():
    fp = _fp(code_revision="explicit-rev")
    assert fp["code_revision"] == "explicit-rev"


def test_settings_code_revision_used_when_no_explicit():
    fp = _fp(settings=_settings(code_revision="from-settings-rev"))
    assert fp["code_revision"] == "from-settings-rev"


def test_verify_fingerprint_names_differing_fields():
    fp1 = _fp()
    fp2 = _fp(source_table_versions={"catalog.schema.expense_report": "99"})
    with pytest.raises(FingerprintMismatch) as exc:
        verify_fingerprint(fp1, fp2)
    assert "source_table_versions" in exc.value.differing_fields


def test_verify_fingerprint_ignores_created_at():
    fp1 = _fp()
    fp2 = dict(fp1)
    fp2["created_at"] = "some-other-timestamp"
    verify_fingerprint(fp1, fp2)  # should not raise
