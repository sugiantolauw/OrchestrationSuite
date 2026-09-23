"""scripts/deploy_app.py's bundle-building logic, exercised directly against
a throwaway fake repo root (never the real one, and never talking to a
workspace -- these tests never call the Databricks SDK)."""

from __future__ import annotations

from pathlib import Path

import scripts.deploy_app as deploy_app


def _fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "fake_repo"
    for name in ("app", "orchestrator", "skills"):
        d = repo / name
        d.mkdir(parents=True)
        (d / "placeholder.py").write_text("# placeholder\n")
    (repo / "requirements.txt").write_text("dash\npandas\n")  # loose, unpinned
    (repo / "requirements.lock").write_text("dash==2.17.1\npandas==2.2.2\n")  # pinned
    return repo


def test_bundle_requirements_txt_is_byte_identical_to_the_lock(monkeypatch, tmp_path):
    """Non-blocking item 1 (CLAUDE.md P2/P3 gate review): Databricks Apps
    installs dependencies from the bundle's OWN requirements.txt, never
    requirements.lock -- shipping the loose requirements.txt meant
    dependency_lock_hash (hashed from requirements.lock) described a set of
    pinned versions that was never what actually got installed. The
    bundle's requirements.txt must be byte-identical to requirements.lock,
    never the repo's own loose requirements.txt."""
    repo = _fake_repo(tmp_path)
    monkeypatch.setattr(deploy_app, "REPO_ROOT", repo)

    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    deploy_app._build_bundle(bundle_dir, "command:\n  - python\n")

    lock_content = (repo / "requirements.lock").read_text()
    loose_content = (repo / "requirements.txt").read_text()
    assert lock_content != loose_content, "fixture must exercise the loose-vs-pinned distinction"

    bundled_requirements_txt = (bundle_dir / "requirements.txt").read_text()
    bundled_lock = (bundle_dir / "requirements.lock").read_text()
    assert bundled_requirements_txt == lock_content
    assert bundled_requirements_txt != loose_content
    assert bundled_lock == lock_content


def test_bundle_raises_if_lock_file_missing(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)
    (repo / "requirements.lock").unlink()
    monkeypatch.setattr(deploy_app, "REPO_ROOT", repo)

    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    import pytest

    with pytest.raises(SystemExit):
        deploy_app._build_bundle(bundle_dir, "command:\n  - python\n")
