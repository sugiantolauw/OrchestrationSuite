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


# ── main(argv) / --source-code-path (independent review 2026-09-24 item 7) ──


def test_build_parser_has_source_code_path_option():
    args = deploy_app.build_parser().parse_args(["--source-code-path", "/Workspace/Repos/me/app"])
    assert args.source_code_path == "/Workspace/Repos/me/app"
    assert args.dry_run is False


def test_build_parser_defaults():
    args = deploy_app.build_parser().parse_args([])
    assert args.app_name is None
    assert args.source_code_path is None
    assert args.dry_run is False


def test_main_dry_run_returns_zero_and_never_touches_a_workspace(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DBX_CATALOG", "cat")
    monkeypatch.setenv("DBX_SCHEMA", "sch")
    monkeypatch.setenv("DATABRICKS_HOST", "https://x.cloud.databricks.com")
    monkeypatch.setenv("DBX_APP_NAME", "ai-audit-analyst")
    monkeypatch.setenv("AUDIT_TIMEZONE", "Australia/Sydney")
    monkeypatch.delenv("DBX_WAREHOUSE_HTTP_PATH", raising=False)

    exit_code = deploy_app.main(["--dry-run"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "app.yaml" in captured.out
    assert "not touching the workspace" in captured.out


def test_main_dry_run_with_source_code_path_mentions_it(monkeypatch, capsys):
    monkeypatch.setenv("DBX_CATALOG", "cat")
    monkeypatch.setenv("DBX_SCHEMA", "sch")
    monkeypatch.setenv("DATABRICKS_HOST", "https://x.cloud.databricks.com")
    monkeypatch.setenv("DBX_APP_NAME", "ai-audit-analyst")
    monkeypatch.setenv("AUDIT_TIMEZONE", "Australia/Sydney")
    monkeypatch.delenv("DBX_WAREHOUSE_HTTP_PATH", raising=False)

    exit_code = deploy_app.main(["--dry-run", "--source-code-path", "/Workspace/Repos/me/app"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "/Workspace/Repos/me/app/app.yaml" in captured.out


def test_main_raises_without_app_name(monkeypatch):
    import pytest

    monkeypatch.setenv("DBX_CATALOG", "cat")
    monkeypatch.setenv("DBX_SCHEMA", "sch")
    monkeypatch.setenv("DATABRICKS_HOST", "https://x.cloud.databricks.com")
    monkeypatch.delenv("DBX_APP_NAME", raising=False)

    with pytest.raises(SystemExit):
        deploy_app.main(["--dry-run"])


# ── §11 "Paused runs across a code deploy" / independent review 2026-09-24
# gap #11: a warning before deploying, never a block, listing every paused
# or running run so whoever is deploying can see what this deploy may
# affect. ────────────────────────────────────────────────────────────────


class _FakePersistenceForDeployWarning:
    """A minimal stand-in for PersistenceAdapter -- only the two methods
    _warn_about_active_runs actually calls, never a real workspace
    connection (this test never touches the Databricks SDK, matching the
    module docstring's own rule for this file)."""

    def __init__(self, rows, fingerprints):
        self._rows = rows
        self._fingerprints = fingerprints

    def list_runs(self, filters=None):
        return self._rows

    def get_fingerprints(self, fingerprint_ids):
        return {fid: self._fingerprints[fid] for fid in fingerprint_ids if fid in self._fingerprints}


def test_warn_about_active_runs_lists_paused_and_running_runs(capsys):
    rows = [
        {"run_id": "RUN-PAUSED", "status": "awaiting_signoff", "phase": "execute", "fingerprint_id": "FP-1"},
        {"run_id": "RUN-RUNNING", "status": "running", "phase": "execute", "fingerprint_id": "FP-2"},
        {"run_id": "RUN-DONE", "status": "completed", "phase": "export", "fingerprint_id": "FP-3"},
    ]
    fingerprints = {
        "FP-1": {"code_revision": "rev-old-1"},
        "FP-2": {"code_revision": "rev-old-2"},
        "FP-3": {"code_revision": "rev-old-3"},
    }
    persistence = _FakePersistenceForDeployWarning(rows, fingerprints)

    deploy_app._warn_about_active_runs(persistence)

    out = capsys.readouterr().out
    assert "RUN-PAUSED" in out
    assert "rev-old-1" in out
    assert "RUN-RUNNING" in out
    assert "rev-old-2" in out
    # A completed run is not paused or running -- not this deploy's concern.
    assert "RUN-DONE" not in out


def test_warn_about_active_runs_prints_nothing_when_nothing_is_active(capsys):
    persistence = _FakePersistenceForDeployWarning(
        [{"run_id": "RUN-DONE", "status": "completed", "phase": "export", "fingerprint_id": "FP-1"}],
        {"FP-1": {"code_revision": "rev-1"}},
    )
    deploy_app._warn_about_active_runs(persistence)
    assert capsys.readouterr().out == ""


def test_warn_about_active_runs_never_raises_on_a_persistence_error(capsys):
    class _BrokenPersistence:
        def list_runs(self, filters=None):
            raise RuntimeError("warehouse unreachable")

    deploy_app._warn_about_active_runs(_BrokenPersistence())
    out = capsys.readouterr().out
    assert "Could not check" in out


# ── BUG-1/BUG-2 fix (final test round, TEST_REPORT_stage345.md): every
# runtime setting orchestrator.config.Settings reads is forwarded into
# app.yaml, secrets never are, and an enabled feature missing a setting it
# needs fails the deploy loudly instead of shipping silently degraded. ─────


def _settings(**overrides):
    from orchestrator.config import Settings

    base = dict(
        catalog="cat", schema="sch", host="https://x.cloud.databricks.com",
        app_name="ai-audit-analyst", audit_timezone="Australia/Sydney",
    )
    base.update(overrides)
    return Settings(**base)


def test_check_feature_requirements_flags_missing_audit_timezone():
    problems = deploy_app._check_feature_requirements(_settings(audit_timezone=None))
    assert any("AUDIT_TIMEZONE" in p for p in problems)


def test_check_feature_requirements_passes_with_audit_timezone_set():
    assert deploy_app._check_feature_requirements(_settings()) == []


def test_check_feature_requirements_flags_missing_endpoints_when_narration_enabled():
    problems = deploy_app._check_feature_requirements(
        _settings(narration_enabled=True, model_sonnet=None, model_gpt_oss=None)
    )
    assert any("MODEL_SONNET" in p and "NARRATION_ENABLED" in p for p in problems)
    assert any("MODEL_GPT_OSS" in p and "NARRATION_ENABLED" in p for p in problems)


def test_check_feature_requirements_passes_when_narration_enabled_with_both_endpoints():
    problems = deploy_app._check_feature_requirements(
        _settings(narration_enabled=True, model_sonnet="s", model_gpt_oss="g")
    )
    assert problems == []


def test_check_feature_requirements_flags_ai_proposed_without_narration():
    problems = deploy_app._check_feature_requirements(
        _settings(narration_enabled=False, ai_proposed_findings_enabled=True)
    )
    assert any("AI_PROPOSED_FINDINGS_ENABLED" in p and "NARRATION_ENABLED" in p for p in problems)


def test_check_feature_requirements_passes_when_both_narration_flags_enabled_together():
    problems = deploy_app._check_feature_requirements(
        _settings(narration_enabled=True, ai_proposed_findings_enabled=True,
                  model_sonnet="s", model_gpt_oss="g")
    )
    assert problems == []


def test_runtime_settings_env_vars_forwards_narration_and_timezone_settings():
    settings = _settings(narration_enabled=True, ai_proposed_findings_enabled=True,
                          model_sonnet="s", model_gpt_oss="g")
    out = deploy_app._runtime_settings_env_vars(settings, {})
    assert out["AUDIT_TIMEZONE"] == "Australia/Sydney"
    assert out["NARRATION_ENABLED"] == "true"
    assert out["AI_PROPOSED_FINDINGS_ENABLED"] == "true"
    assert out["EXECUTOR"] == "thread"
    assert out["MAX_CONCURRENT_RUNS"] == "2"
    assert out["DEMO_MODE"] == "false"


def test_runtime_settings_env_vars_omits_unset_optional_settings():
    out = deploy_app._runtime_settings_env_vars(_settings(), {})
    assert "SOURCE_BINDINGS" not in out
    assert "PPTX_TEMPLATE_PATH" not in out
    assert "LLM_MONTHLY_TOKEN_BUDGET" not in out
    assert "PII_TAG_NAMES" not in out


def test_runtime_settings_env_vars_forwards_a_customised_pptx_template_path():
    out = deploy_app._runtime_settings_env_vars(_settings(), {"PPTX_TEMPLATE_PATH": "/custom/template.pptx"})
    assert out["PPTX_TEMPLATE_PATH"] == "/custom/template.pptx"


def test_runtime_settings_env_vars_forwards_raw_passthrough_settings():
    out = deploy_app._runtime_settings_env_vars(_settings(), {"DBX_MAX_CELLS": "5000000", "MAX_UPLOAD_MB": "50"})
    assert out["DBX_MAX_CELLS"] == "5000000"
    assert out["MAX_UPLOAD_MB"] == "50"


def test_runtime_settings_env_vars_never_includes_a_token_or_secret():
    """CLAUDE.md §10: never write a token to source. DATABRICKS_TOKEN must
    never appear in app.yaml -- Databricks Apps authenticate as their own
    platform-injected service principal, never this deploying operator's
    PAT, even if this process's own environment happens to have one set."""
    raw_env = {"DATABRICKS_TOKEN": "dapi-super-secret-value"}
    out = deploy_app._runtime_settings_env_vars(_settings(), raw_env)
    assert "DATABRICKS_TOKEN" not in out
    assert "dapi-super-secret-value" not in out.values()


# ── Independent review 2026-09-25 item 3: REVIEW_* settings are forwarded to
# the deployed App, and REVIEW_ROLE_SOURCE=workspace_groups is checked
# against the real workspace at deploy time. ────────────────────────────────


def test_runtime_settings_env_vars_forwards_review_settings():
    settings = _settings(
        review_sod_mode="labelled", review_role_source="workspace_groups",
        review_preparer_groups=("audit-preparers",), review_reviewer_groups=("audit-reviewers",),
        review_approver_groups=("audit-approvers",),
    )
    out = deploy_app._runtime_settings_env_vars(settings, {})
    assert out["REVIEW_SOD_MODE"] == "labelled"
    assert out["REVIEW_ROLE_SOURCE"] == "workspace_groups"
    assert out["REVIEW_PREPARER_GROUPS"] == "audit-preparers"
    assert out["REVIEW_REVIEWER_GROUPS"] == "audit-reviewers"
    assert out["REVIEW_APPROVER_GROUPS"] == "audit-approvers"


def test_runtime_settings_env_vars_forwards_review_settings_using_code_defaults_when_unset():
    """An unset REVIEW_* in this deploying process's own .env still forwards
    the same code-level default the deployed App would otherwise compute for
    itself (matching audit_timezone/narration_enabled's own reasoning) --
    never omitted, and an empty group tuple forwards as the empty string,
    never invented here."""
    out = deploy_app._runtime_settings_env_vars(_settings(), {})
    assert out["REVIEW_SOD_MODE"] == "enforced"
    assert out["REVIEW_ROLE_SOURCE"] == "workspace_groups"
    assert out["REVIEW_PREPARER_GROUPS"] == ""
    assert out["REVIEW_REVIEWER_GROUPS"] == ""
    assert out["REVIEW_APPROVER_GROUPS"] == ""


def test_runtime_settings_env_vars_omits_review_role_assignments_when_unset():
    assert "REVIEW_ROLE_ASSIGNMENTS" not in deploy_app._runtime_settings_env_vars(_settings(), {})


def test_runtime_settings_env_vars_forwards_review_role_assignments_when_set():
    settings = _settings(review_role_assignments_path="/gitignored/review_roles.yaml")
    out = deploy_app._runtime_settings_env_vars(settings, {})
    assert out["REVIEW_ROLE_ASSIGNMENTS"] == "/gitignored/review_roles.yaml"


class _FakeGroup:
    def __init__(self, display_name, group_id="grp-1"):
        self.display_name = display_name
        self.id = group_id


class _FakeGroupsAPI:
    def __init__(self, group_names):
        self._groups = [_FakeGroup(name, f"grp-{i}") for i, name in enumerate(group_names)]

    def list(self):
        return list(self._groups)


class _FakeWorkspaceClientForGroups:
    def __init__(self, group_names):
        self.groups = _FakeGroupsAPI(group_names)


def _review_settings(**overrides):
    base = dict(
        review_role_source="workspace_groups",
        review_preparer_groups=("audit-preparers",), review_reviewer_groups=("audit-reviewers",),
        review_approver_groups=("audit-approvers",),
    )
    base.update(overrides)
    return _settings(**base)


def test_check_review_groups_exist_passes_when_every_group_is_present():
    w = _FakeWorkspaceClientForGroups(["audit-preparers", "audit-reviewers", "audit-approvers", "some-other-group"])
    assert deploy_app._check_review_groups_exist(w, _review_settings()) == []


def test_check_review_groups_exist_reports_every_missing_group():
    w = _FakeWorkspaceClientForGroups(["audit-preparers"])
    problems = deploy_app._check_review_groups_exist(w, _review_settings())
    assert any("audit-reviewers" in p for p in problems)
    assert any("audit-approvers" in p for p in problems)
    assert not any("audit-preparers" in p for p in problems)


def test_check_review_groups_exist_skips_the_check_for_config_role_source():
    w = _FakeWorkspaceClientForGroups([])
    settings = _review_settings(review_role_source="config")
    assert deploy_app._check_review_groups_exist(w, settings) == []


def test_check_review_groups_exist_skips_the_check_when_no_groups_are_configured():
    w = _FakeWorkspaceClientForGroups([])
    settings = _settings(
        review_role_source="workspace_groups",
        review_preparer_groups=(), review_reviewer_groups=(), review_approver_groups=(),
    )
    assert deploy_app._check_review_groups_exist(w, settings) == []


def test_check_review_groups_exist_never_calls_groups_create():
    """"don't create them in the script" -- the deploy-time check only
    reads; scripts/ensure_review_groups.py is the separate, explicitly-run
    step that creates a missing group."""
    w = _FakeWorkspaceClientForGroups(["audit-preparers"])
    assert not hasattr(w.groups, "create")
    deploy_app._check_review_groups_exist(w, _review_settings())


def test_main_dry_run_fails_loudly_when_narration_enabled_without_endpoints(monkeypatch):
    import pytest

    monkeypatch.setenv("DBX_CATALOG", "cat")
    monkeypatch.setenv("DBX_SCHEMA", "sch")
    monkeypatch.setenv("DATABRICKS_HOST", "https://x.cloud.databricks.com")
    monkeypatch.setenv("DBX_APP_NAME", "ai-audit-analyst")
    monkeypatch.setenv("AUDIT_TIMEZONE", "Australia/Sydney")
    monkeypatch.setenv("NARRATION_ENABLED", "true")
    monkeypatch.delenv("MODEL_SONNET", raising=False)
    monkeypatch.delenv("MODEL_GPT_OSS", raising=False)
    monkeypatch.delenv("DBX_WAREHOUSE_HTTP_PATH", raising=False)

    with pytest.raises(SystemExit, match="MODEL_SONNET"):
        deploy_app.main(["--dry-run"])


def test_main_dry_run_fails_loudly_when_audit_timezone_unset(monkeypatch):
    import pytest

    monkeypatch.setenv("DBX_CATALOG", "cat")
    monkeypatch.setenv("DBX_SCHEMA", "sch")
    monkeypatch.setenv("DATABRICKS_HOST", "https://x.cloud.databricks.com")
    monkeypatch.setenv("DBX_APP_NAME", "ai-audit-analyst")
    monkeypatch.delenv("AUDIT_TIMEZONE", raising=False)
    monkeypatch.delenv("DBX_WAREHOUSE_HTTP_PATH", raising=False)

    with pytest.raises(SystemExit, match="AUDIT_TIMEZONE"):
        deploy_app.main(["--dry-run"])


def test_main_dry_run_app_yaml_includes_forwarded_settings_and_no_token(monkeypatch, capsys):
    monkeypatch.setenv("DBX_CATALOG", "cat")
    monkeypatch.setenv("DBX_SCHEMA", "sch")
    monkeypatch.setenv("DATABRICKS_HOST", "https://x.cloud.databricks.com")
    monkeypatch.setenv("DBX_APP_NAME", "ai-audit-analyst")
    monkeypatch.setenv("AUDIT_TIMEZONE", "Australia/Sydney")
    monkeypatch.setenv("NARRATION_ENABLED", "true")
    monkeypatch.setenv("AI_PROPOSED_FINDINGS_ENABLED", "true")
    monkeypatch.setenv("MODEL_SONNET", "sonnet-endpoint")
    monkeypatch.setenv("MODEL_GPT_OSS", "gptoss-endpoint")
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-should-never-appear")
    monkeypatch.delenv("DBX_WAREHOUSE_HTTP_PATH", raising=False)

    exit_code = deploy_app.main(["--dry-run"])
    assert exit_code == 0
    app_yaml = capsys.readouterr().out

    for expected in (
        "name: AUDIT_TIMEZONE", 'value: "Australia/Sydney"',
        "name: NARRATION_ENABLED", 'value: "true"',
        "name: AI_PROPOSED_FINDINGS_ENABLED",
        "name: EXECUTOR", 'value: "thread"',
        "name: MAX_CONCURRENT_RUNS", 'value: "2"',
    ):
        assert expected in app_yaml, app_yaml

    assert "DATABRICKS_TOKEN" not in app_yaml
    assert "dapi-should-never-appear" not in app_yaml


# ── BUG-DEPLOY-1: the App must be started before w.apps.deploy() will
# accept it, and CLAUDE.md's own cost-saving guidance (§11) leaves the App
# STOPPED between sessions -- deploy_app.py must start it itself, not fail
# and tell the operator to. ──────────────────────────────────────────────


class _FakeComputeStatus:
    def __init__(self, state):
        self.state = state


class _FakeApp:
    def __init__(self, *, state, name="ai-audit-analyst", url="https://example.databricksapps.com"):
        from databricks.sdk.service.apps import ComputeState

        self.name = name
        self.url = url
        self.service_principal_client_id = "sp-client-id"
        self.service_principal_id = None
        self.service_principal_name = None
        self.compute_status = _FakeComputeStatus(state) if state is not None else None
        self._ComputeState = ComputeState


class _FakeWait:
    """Mirrors the SDK's Wait[T] -- .result(timeout=...) returns the final
    value; some SDK calls (App.create) instead return the value directly,
    which is why deploy_app.py's own code guards with hasattr(wait, "result")."""

    def __init__(self, value):
        self._value = value

    def result(self, timeout=None):
        return self._value


def test_ensure_app_started_starts_a_stopped_app_and_waits_for_active(capsys):
    from databricks.sdk.service.apps import ComputeState

    class _FakeAppsAPI:
        def __init__(self):
            self.start_calls = []

        def start(self, name):
            self.start_calls.append(name)
            return _FakeWait(_FakeApp(state=ComputeState.ACTIVE))

        def get(self, name):
            return _FakeApp(state=ComputeState.ACTIVE)

    apps_api = _FakeAppsAPI()
    w = type("W", (), {"apps": apps_api})()

    stopped_app = _FakeApp(state=ComputeState.STOPPED)
    result = deploy_app._ensure_app_started(w, "ai-audit-analyst", stopped_app)

    assert apps_api.start_calls == ["ai-audit-analyst"]
    assert result.compute_status.state == ComputeState.ACTIVE
    out = capsys.readouterr().out
    assert "STOPPED" in out
    assert "starting it" in out
    assert "$0.58/h" in out
    assert "now ACTIVE" in out


def test_ensure_app_started_is_a_noop_when_already_active(capsys):
    from databricks.sdk.service.apps import ComputeState

    class _FakeAppsAPI:
        def __init__(self):
            self.start_calls = []

        def start(self, name):  # pragma: no cover - must not be called
            self.start_calls.append(name)
            raise AssertionError("must not start an App that is already ACTIVE")

    apps_api = _FakeAppsAPI()
    w = type("W", (), {"apps": apps_api})()

    active_app = _FakeApp(state=ComputeState.ACTIVE)
    result = deploy_app._ensure_app_started(w, "ai-audit-analyst", active_app)

    assert apps_api.start_calls == []
    assert result is active_app
    assert capsys.readouterr().out == ""


def test_ensure_app_started_leaves_a_transient_state_alone(capsys):
    """STARTING/STOPPING/UPDATING are already in flight -- only a genuinely
    STOPPED App needs this function to act; anything else is left for
    w.apps.deploy() itself to accept or reject."""
    from databricks.sdk.service.apps import ComputeState

    class _FakeAppsAPI:
        def start(self, name):  # pragma: no cover - must not be called
            raise AssertionError("must not start an App that is not STOPPED")

    w = type("W", (), {"apps": _FakeAppsAPI()})()

    starting_app = _FakeApp(state=ComputeState.STARTING)
    result = deploy_app._ensure_app_started(w, "ai-audit-analyst", starting_app)

    assert result is starting_app
    assert capsys.readouterr().out == ""


def test_build_parser_has_stop_after_flag_defaulting_to_false():
    args = deploy_app.build_parser().parse_args([])
    assert args.stop_after is False


def test_build_parser_stop_after_flag_can_be_set():
    args = deploy_app.build_parser().parse_args(["--stop-after"])
    assert args.stop_after is True


# ── _finish_deploy end to end (mocked SDK client, no workspace) -- covers
# BUG-DEPLOY-1's start-before-deploy path and the --stop-after path
# together, since both live in the same function. ─────────────────────────


class _FakeGrantsAPI:
    def update(self, securable_type, full_name, *, changes=None):
        pass


class _FakeAppsAPIFull:
    """A fuller fake of AppsAPI covering everything _finish_deploy touches:
    list/create_update_and_wait/get (via _ensure_app, an existing App),
    start (BUG-DEPLOY-1), deploy, and stop (--stop-after)."""

    def __init__(self, *, initial_state, deploy_state, post_deploy_state=None):
        from databricks.sdk.service.apps import ComputeState

        self._state = initial_state
        self._deploy_state = deploy_state
        self._post_deploy_state = post_deploy_state if post_deploy_state is not None else ComputeState.ACTIVE
        self.start_calls = []
        self.stop_calls = []
        self.deploy_calls = []

    def list(self):
        return [_FakeApp(state=self._state)]

    def create_update_and_wait(self, name, *, update_mask=None, app=None, timeout=None):
        return _FakeApp(state=self._state, name=name)

    def get(self, name):
        return _FakeApp(state=self._state, name=name)

    def start(self, name):
        from databricks.sdk.service.apps import ComputeState

        self.start_calls.append(name)
        self._state = ComputeState.ACTIVE
        return _FakeWait(_FakeApp(state=self._state, name=name))

    def deploy(self, name, deployment):
        from databricks.sdk.service.apps import AppDeployment, AppDeploymentStatus

        self.deploy_calls.append(name)
        self._state = self._post_deploy_state
        result = AppDeployment(deployment_id="dep-1", status=AppDeploymentStatus(state=self._deploy_state))
        return _FakeWait(result)

    def stop(self, name):
        from databricks.sdk.service.apps import ComputeState

        self.stop_calls.append(name)
        self._state = ComputeState.STOPPED
        return _FakeWait(_FakeApp(state=self._state, name=name))


def _fake_workspace_client_for_finish_deploy(apps_api):
    experiments = _FakeExperimentsAPI(existing_path_to_id={"/Shared/ai-audit-analyst-audit-runs": "exp-1"})
    return type("W", (), {"apps": apps_api, "grants": _FakeGrantsAPI(), "experiments": experiments})()


def _finish_deploy_settings():
    from orchestrator.config import Settings

    return Settings(catalog="cat", schema="sch", host="https://x.cloud.databricks.com", app_name="ai-audit-analyst")


def test_finish_deploy_starts_a_stopped_app_before_deploying(capsys):
    from databricks.sdk.service.apps import AppDeploymentState, ComputeState

    apps_api = _FakeAppsAPIFull(initial_state=ComputeState.STOPPED, deploy_state=AppDeploymentState.SUCCEEDED)
    w = _fake_workspace_client_for_finish_deploy(apps_api)

    deploy_app._finish_deploy(
        w, app_name="ai-audit-analyst", warehouse_id="wh-1", workspace_dir="/Workspace/Users/me/ai-audit-analyst",
        settings=_finish_deploy_settings(), source_schemas=[],
        env_vars={"MLFLOW_EXPERIMENT_PATH": "/Shared/ai-audit-analyst-audit-runs"},
    )

    assert apps_api.start_calls == ["ai-audit-analyst"]
    assert apps_api.deploy_calls == ["ai-audit-analyst"]
    assert apps_api.stop_calls == []  # stop_after defaults to False
    out = capsys.readouterr().out
    assert "starting it" in out
    assert "Deployed" in out


def test_finish_deploy_stop_after_stops_the_app_once_deployment_succeeds(capsys):
    from databricks.sdk.service.apps import AppDeploymentState, ComputeState

    apps_api = _FakeAppsAPIFull(
        initial_state=ComputeState.STOPPED, deploy_state=AppDeploymentState.SUCCEEDED,
        post_deploy_state=ComputeState.ACTIVE,
    )
    w = _fake_workspace_client_for_finish_deploy(apps_api)

    deploy_app._finish_deploy(
        w, app_name="ai-audit-analyst", warehouse_id="wh-1", workspace_dir="/Workspace/Users/me/ai-audit-analyst",
        settings=_finish_deploy_settings(), source_schemas=[],
        env_vars={"MLFLOW_EXPERIMENT_PATH": "/Shared/ai-audit-analyst-audit-runs"},
        stop_after=True,
    )

    assert apps_api.start_calls == ["ai-audit-analyst"]
    assert apps_api.stop_calls == ["ai-audit-analyst"]
    out = capsys.readouterr().out
    assert "stopping it now" in out
    assert "now STOPPED" in out


def test_finish_deploy_stop_after_does_not_stop_when_deployment_did_not_succeed(capsys):
    from databricks.sdk.service.apps import AppDeploymentState, ComputeState

    apps_api = _FakeAppsAPIFull(
        initial_state=ComputeState.STOPPED, deploy_state=AppDeploymentState.FAILED,
        post_deploy_state=ComputeState.ACTIVE,
    )
    w = _fake_workspace_client_for_finish_deploy(apps_api)

    deploy_app._finish_deploy(
        w, app_name="ai-audit-analyst", warehouse_id="wh-1", workspace_dir="/Workspace/Users/me/ai-audit-analyst",
        settings=_finish_deploy_settings(), source_schemas=[],
        env_vars={"MLFLOW_EXPERIMENT_PATH": "/Shared/ai-audit-analyst-audit-runs"},
        stop_after=True,
    )

    assert apps_api.stop_calls == []
    out = capsys.readouterr().out
    assert "not stopping" in out
