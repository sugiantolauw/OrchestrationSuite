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


# ── MLflow experiment permissions (MLflow-on-the-platform item) ─────────────


class _FakeExperiment:
    def __init__(self, experiment_id):
        self.experiment_id = experiment_id


class _FakeGetByNameResponse:
    def __init__(self, experiment_id):
        self.experiment = _FakeExperiment(experiment_id)


class _FakeCreateExperimentResponse:
    def __init__(self, experiment_id):
        self.experiment_id = experiment_id


class _FakeExperimentsAPI:
    """Mirrors the real databricks.sdk.service.ml.ExperimentsAPI's exact
    method names/signatures/exceptions (verified against the installed SDK
    directly: get_by_name, create_experiment, update_permissions,
    ExperimentAccessControlRequest, ExperimentPermissionLevel) -- never runs
    against a real workspace."""

    def __init__(self, *, existing_path_to_id=None, fail_create_with_already_exists=False, reveal_after_calls=0):
        self._by_path = dict(existing_path_to_id or {})
        self._fail_create_with_already_exists = fail_create_with_already_exists
        # Simulates a concurrent winner: the entry only becomes visible to
        # get_by_name once this many prior get_by_name calls have happened,
        # so the FIRST lookup can genuinely miss (NotFound) while a LATER
        # one (after losing the create race) succeeds.
        self._reveal_after_calls = reveal_after_calls
        self._get_by_name_calls = 0
        self.update_permissions_calls = []
        self.create_experiment_calls = []

    def get_by_name(self, experiment_name):
        from databricks.sdk.errors import NotFound

        self._get_by_name_calls += 1
        visible = experiment_name in self._by_path and self._get_by_name_calls > self._reveal_after_calls
        if not visible:
            raise NotFound(f"no experiment named {experiment_name!r}")
        return _FakeGetByNameResponse(self._by_path[experiment_name])

    def create_experiment(self, name, **kwargs):
        from databricks.sdk.errors import ResourceAlreadyExists

        self.create_experiment_calls.append(name)
        if self._fail_create_with_already_exists:
            raise ResourceAlreadyExists(f"{name!r} already exists")
        new_id = f"exp-{len(self._by_path) + 1}"
        self._by_path[name] = new_id
        return _FakeCreateExperimentResponse(new_id)

    def update_permissions(self, experiment_id, *, access_control_list=None):
        self.update_permissions_calls.append((experiment_id, access_control_list))


class _FakeWorkspaceClient:
    def __init__(self, experiments_api):
        self.experiments = experiments_api


def test_ensure_mlflow_experiment_permissions_grants_on_existing_experiment():
    experiments = _FakeExperimentsAPI(existing_path_to_id={"/Shared/app-audit-runs": "exp-existing"})
    w = _FakeWorkspaceClient(experiments)

    deploy_app._ensure_mlflow_experiment_permissions(w, "/Shared/app-audit-runs", "principal-123")

    assert experiments.create_experiment_calls == []
    assert len(experiments.update_permissions_calls) == 1
    experiment_id, acl = experiments.update_permissions_calls[0]
    assert experiment_id == "exp-existing"
    assert len(acl) == 1
    assert acl[0].service_principal_name == "principal-123"
    from databricks.sdk.service.ml import ExperimentPermissionLevel

    assert acl[0].permission_level == ExperimentPermissionLevel.CAN_MANAGE


def test_ensure_mlflow_experiment_permissions_creates_experiment_if_missing():
    experiments = _FakeExperimentsAPI()
    w = _FakeWorkspaceClient(experiments)

    deploy_app._ensure_mlflow_experiment_permissions(w, "/Shared/new-audit-runs", "principal-123")

    assert experiments.create_experiment_calls == ["/Shared/new-audit-runs"]
    assert len(experiments.update_permissions_calls) == 1
    experiment_id, acl = experiments.update_permissions_calls[0]
    assert experiment_id == experiments._by_path["/Shared/new-audit-runs"]
    assert acl[0].service_principal_name == "principal-123"


def test_ensure_mlflow_experiment_permissions_handles_concurrent_creation_race():
    # Two deploys racing to create the same fresh experiment: create_experiment
    # loses the race (ResourceAlreadyExists) -- must fall back to get_by_name
    # for the id rather than raising.
    experiments = _FakeExperimentsAPI(
        existing_path_to_id={"/Shared/race-audit-runs": "exp-winner"},
        fail_create_with_already_exists=True, reveal_after_calls=1,
    )
    w = _FakeWorkspaceClient(experiments)

    deploy_app._ensure_mlflow_experiment_permissions(w, "/Shared/race-audit-runs", "principal-123")

    assert experiments.create_experiment_calls == ["/Shared/race-audit-runs"]
    experiment_id, _ = experiments.update_permissions_calls[0]
    assert experiment_id == "exp-winner"
