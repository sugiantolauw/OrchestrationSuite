from __future__ import annotations

import hashlib
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


def test_changes_when_the_lock_file_changes(tmp_path):
    # P2/P3 gate review item 8: dependency_lock_hash hashes the LOCK
    # (requirements.txt's sibling .lock, the transitive closure pinned
    # exactly), not requirements.txt itself -- two runs with an IDENTICAL
    # requirements.txt but a DIFFERENT lock (e.g. a transitive package
    # bumped without requirements.txt itself changing) must get different
    # fingerprints.
    req1 = tmp_path / "requirements1.txt"
    req1.write_text("pandas>=2.1\n")
    (tmp_path / "requirements1.lock").write_text("pandas==2.1.0\nnumpy==1.26.0\n")
    req2 = tmp_path / "requirements2.txt"
    req2.write_text("pandas>=2.1\n")
    (tmp_path / "requirements2.lock").write_text("pandas==2.2.0\nnumpy==1.26.0\n")
    fp1 = _fp(requirements_path=req1)
    fp2 = _fp(requirements_path=req2)
    assert fp1["fingerprint_id"] != fp2["fingerprint_id"]
    assert fp1["dependency_lock_hash"] != fp2["dependency_lock_hash"]


def test_lock_hash_unaffected_by_requirements_txt_alone(tmp_path):
    # The converse of the above: requirements.txt DIFFERING with the SAME
    # lock content produces the SAME dependency_lock_hash -- proves the lock
    # file, not requirements.txt, is what is actually hashed.
    req1 = tmp_path / "requirements1.txt"
    req1.write_text("pandas>=2.1\n")
    (tmp_path / "requirements1.lock").write_text("pandas==2.1.0\n")
    req2 = tmp_path / "requirements2.txt"
    req2.write_text("pandas>=2.1\nnumpy>=1.26\n")  # requirements.txt differs
    (tmp_path / "requirements2.lock").write_text("pandas==2.1.0\n")  # lock does not
    fp1 = _fp(requirements_path=req1)
    fp2 = _fp(requirements_path=req2)
    assert fp1["dependency_lock_hash"] == fp2["dependency_lock_hash"]


def test_missing_lock_file_raises_never_falls_back_to_requirements_txt(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("pandas>=2.1\n")  # no sibling requirements.lock written
    with pytest.raises(ConfigError, match="dependency_lock_hash"):
        _fp(requirements_path=req)


def test_explicit_dependency_lock_path_overrides_the_sibling_convention(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("pandas>=2.1\n")
    lock = tmp_path / "elsewhere" / "the.lock"
    lock.parent.mkdir()
    lock.write_text("pandas==2.1.0\n")
    fp = _fp(requirements_path=req, dependency_lock_path=lock)
    assert fp["dependency_lock_hash"] == hashlib.sha256(lock.read_bytes()).hexdigest()


def test_changes_when_config_value_changes():
    fp1 = _fp(settings=_settings(demo_mode=False))
    fp2 = _fp(settings=_settings(demo_mode=True))
    assert fp1["fingerprint_id"] != fp2["fingerprint_id"]
    assert fp1["runtime_config_hash"] != fp2["runtime_config_hash"]


def test_unchanged_when_operational_knobs_change():
    # max_concurrent_runs and executor are scheduling knobs, not part of what a run
    # computes -- excluded from runtime_config_hash (CLAUDE.md §4.1).
    fp1 = _fp(settings=_settings(max_concurrent_runs=2, executor="thread"))
    fp2 = _fp(settings=_settings(max_concurrent_runs=6, executor="jobs"))
    assert fp1["fingerprint_id"] == fp2["fingerprint_id"]


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


def test_changes_when_reference_file_byte_changes(tmp_path):
    ref = tmp_path / "sgd_aud_rate.csv"
    ref.write_text("rate,effective_date\n0.90,2026-01-01\n")
    fp1 = _fp(reference_files=[ref])

    ref.write_text("rate,effective_date\n0.91,2026-01-01\n")
    fp2 = _fp(reference_files=[ref])

    assert fp1["fingerprint_id"] != fp2["fingerprint_id"]
    assert fp1["reference_data_hashes"] != fp2["reference_data_hashes"]


def test_reference_data_hashes_empty_when_absent():
    fp = _fp(reference_files=None)
    assert json.loads(fp["reference_data_hashes"]) == {}


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


def _init_git_repo(root, monkeypatch):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "tracked.txt").write_text("original\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)

    import orchestrator.fingerprint as fp_mod

    monkeypatch.setattr(fp_mod, "_REPO_ROOT", root)
    return fp_mod


def test_dirty_code_revision_includes_a_hash_of_the_actual_diff(tmp_path, monkeypatch):
    """Independent review 2026-09-24 item 7: a bare "+dirty" suffix made
    every dirty working tree at the same HEAD indistinguishable to the
    fingerprint, regardless of what actually changed. Two DIFFERENT dirty
    diffs at the SAME commit must resolve to two DIFFERENT code_revision
    strings, both still prefixed by the same commit hash and "+dirty."."""
    fp_mod = _init_git_repo(tmp_path, monkeypatch)
    settings = fp_mod.Settings()

    (tmp_path / "tracked.txt").write_text("changed one way\n")
    rev_a = fp_mod._resolve_code_revision(None, settings.code_revision)

    (tmp_path / "tracked.txt").write_text("changed a DIFFERENT way\n")
    rev_b = fp_mod._resolve_code_revision(None, settings.code_revision)

    assert rev_a != rev_b
    commit_hash = rev_a.split("+dirty.")[0]
    assert rev_b.startswith(commit_hash)
    assert "+dirty." in rev_a and "+dirty." in rev_b


def test_identical_dirty_diff_is_deterministic(tmp_path, monkeypatch):
    fp_mod = _init_git_repo(tmp_path, monkeypatch)
    settings = fp_mod.Settings()

    (tmp_path / "tracked.txt").write_text("same change\n")
    rev_1 = fp_mod._resolve_code_revision(None, settings.code_revision)
    rev_2 = fp_mod._resolve_code_revision(None, settings.code_revision)
    assert rev_1 == rev_2


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


# ── §11 "Paused runs across a code deploy" / independent review 2026-09-24
# gap #11: code_revision may differ ONLY when the caller explicitly says so
# (the export phase of a run whose execute phase already completed) --
# every other hashed field must still match exactly regardless.


def test_verify_fingerprint_rejects_code_revision_diff_by_default():
    fp1 = _fp(settings=_settings(code_revision="rev-old"))
    fp2 = _fp(settings=_settings(code_revision="rev-new"))
    with pytest.raises(FingerprintMismatch) as exc:
        verify_fingerprint(fp1, fp2)
    assert "code_revision" in exc.value.differing_fields


def test_verify_fingerprint_allows_code_revision_diff_when_told_to():
    fp1 = _fp(settings=_settings(code_revision="rev-old"))
    fp2 = _fp(settings=_settings(code_revision="rev-new"))
    verify_fingerprint(fp1, fp2, allow_code_revision_diff=True)  # should not raise


def test_verify_fingerprint_allow_code_revision_diff_still_enforces_every_other_field(tmp_path):
    """The relaxation is code_revision ONLY -- changing the Skill content
    hash too (a stand-in for any other hashed field, e.g. the Skill's
    contract.yaml) must still fail even with allow_code_revision_diff=True."""
    skill_dir_a = tmp_path / "skill_a"
    skill_dir_a.mkdir()
    (skill_dir_a / "manifest.yaml").write_text("id: SKILL-A\n")
    skill_dir_b = tmp_path / "skill_b"
    skill_dir_b.mkdir()
    (skill_dir_b / "manifest.yaml").write_text("id: SKILL-B\n")

    fp1 = _fp(settings=_settings(code_revision="rev-old"), skill_dir=skill_dir_a)
    fp2 = _fp(settings=_settings(code_revision="rev-new"), skill_dir=skill_dir_b)
    with pytest.raises(FingerprintMismatch) as exc:
        verify_fingerprint(fp1, fp2, allow_code_revision_diff=True)
    assert "skill_content_hash" in exc.value.differing_fields
    assert "code_revision" not in exc.value.differing_fields
