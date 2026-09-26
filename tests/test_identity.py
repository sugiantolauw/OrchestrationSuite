from __future__ import annotations

import pytest

from orchestrator.config import load_settings
from orchestrator.errors import ConfigError, RoleLookupFailed
from orchestrator.identity import (
    ConfigRoleResolver,
    LazyRoleResolver,
    WorkspaceGroupsRoleResolver,
    build_role_resolver,
)


class _FakeGroup:
    def __init__(self, display):
        self.display = display


class _FakeUser:
    def __init__(self, user_name, groups):
        self.userName = user_name
        self.groups = [_FakeGroup(g) for g in groups]


class _FakeUsers:
    def __init__(self, users_by_email):
        self._users_by_email = users_by_email
        self.calls = 0

    def list(self, filter, attributes):
        self.calls += 1
        assert attributes == "userName,groups"
        email = filter.split('"')[1]
        user = self._users_by_email.get(email)
        return [user] if user is not None else []


class _FakeWorkspaceClient:
    def __init__(self, users_by_email):
        self.users = _FakeUsers(users_by_email)


def _settings(**overrides):
    base = dict(
        review_preparer_groups=("audit-preparers",),
        review_reviewer_groups=("audit-reviewers",),
        review_approver_groups=("audit-approvers",),
        review_sod_mode="enforced",
        review_role_source="workspace_groups",
    )
    base.update(overrides)
    return load_settings({}).__class__(**base)


def test_workspace_groups_resolves_multiple_roles():
    client = _FakeWorkspaceClient({"alice@example.invalid": _FakeUser("alice", ["audit-preparers", "audit-reviewers"])})
    resolver = WorkspaceGroupsRoleResolver(_settings(), workspace_client_factory=lambda: client)

    resolution = resolver.roles_for("alice@example.invalid")

    assert resolution.roles == frozenset({"preparer", "reviewer"})
    assert resolution.matched_groups == {"preparer": "audit-preparers", "reviewer": "audit-reviewers"}
    assert resolution.role_source == "workspace_groups"


def test_workspace_groups_no_matching_groups():
    client = _FakeWorkspaceClient({"bob@example.invalid": _FakeUser("bob", ["some-other-group"])})
    resolver = WorkspaceGroupsRoleResolver(_settings(), workspace_client_factory=lambda: client)

    resolution = resolver.roles_for("bob@example.invalid")

    assert resolution.roles == frozenset()


def test_workspace_groups_user_not_found_raises():
    client = _FakeWorkspaceClient({})
    resolver = WorkspaceGroupsRoleResolver(_settings(), workspace_client_factory=lambda: client)

    with pytest.raises(RoleLookupFailed):
        resolver.roles_for("nobody@example.invalid")


def test_workspace_groups_api_error_raises_never_falls_back():
    class _ExplodingUsers:
        def list(self, filter, attributes):
            raise RuntimeError("network down")

    class _ExplodingClient:
        def __init__(self):
            self.users = _ExplodingUsers()

    resolver = WorkspaceGroupsRoleResolver(_settings(), workspace_client_factory=_ExplodingClient)

    with pytest.raises(RoleLookupFailed):
        resolver.roles_for("alice@example.invalid")


def test_workspace_groups_cache_avoids_second_call():
    client = _FakeWorkspaceClient({"alice@example.invalid": _FakeUser("alice", ["audit-preparers"])})
    resolver = WorkspaceGroupsRoleResolver(_settings(), workspace_client_factory=lambda: client)

    resolver.roles_for("alice@example.invalid")
    resolver.roles_for("alice@example.invalid")

    assert client.users.calls == 1


def test_config_resolver_reads_roles(tmp_path):
    path = tmp_path / "roles.yaml"
    path.write_text("alice@example.invalid: [preparer, reviewer]\nbob@example.invalid: [approver]\n")
    resolver = ConfigRoleResolver(str(path))

    resolution = resolver.roles_for("alice@example.invalid")

    assert resolution.roles == frozenset({"preparer", "reviewer"})
    assert resolution.role_source == "config"
    assert resolution.matched_groups == {"preparer": None, "reviewer": None}


def test_config_resolver_unknown_identity_has_no_roles(tmp_path):
    path = tmp_path / "roles.yaml"
    path.write_text("alice@example.invalid: [preparer]\n")
    resolver = ConfigRoleResolver(str(path))

    resolution = resolver.roles_for("nobody@example.invalid")

    assert resolution.roles == frozenset()


def test_config_resolver_missing_file_raises():
    resolver = ConfigRoleResolver("/nonexistent/roles.yaml")

    with pytest.raises(RoleLookupFailed):
        resolver.roles_for("alice@example.invalid")


def test_build_role_resolver_config_requires_path():
    settings = _settings(review_role_source="config", review_role_assignments_path=None)

    with pytest.raises(ConfigError):
        build_role_resolver(settings)


def test_build_role_resolver_config_ok(tmp_path):
    path = tmp_path / "roles.yaml"
    path.write_text("alice@example.invalid: [preparer]\n")
    settings = _settings(review_role_source="config", review_role_assignments_path=str(path))

    resolver = build_role_resolver(settings)

    assert isinstance(resolver, ConfigRoleResolver)


def test_build_role_resolver_enforced_requires_all_groups():
    settings = _settings(review_sod_mode="enforced", review_preparer_groups=())

    with pytest.raises(ConfigError):
        build_role_resolver(settings)


def test_build_role_resolver_labelled_allows_unset_groups():
    settings = _settings(review_sod_mode="labelled", review_preparer_groups=(), review_reviewer_groups=(), review_approver_groups=())

    resolver = build_role_resolver(settings)

    assert isinstance(resolver, WorkspaceGroupsRoleResolver)


def test_build_role_resolver_unknown_source_raises():
    settings = _settings(review_role_source="carrier-pigeon")

    with pytest.raises(ConfigError):
        build_role_resolver(settings)


def test_lazy_role_resolver_does_not_build_until_used():
    # default Settings(): enforced mode, no groups configured -- would raise
    # ConfigError from build_role_resolver eagerly, but LazyRoleResolver must
    # not touch it until roles_for is actually called.
    settings = load_settings({})
    LazyRoleResolver(settings)  # constructing it alone must never raise


def test_lazy_role_resolver_raises_only_on_first_use():
    settings = load_settings({})
    resolver = LazyRoleResolver(settings)

    with pytest.raises(ConfigError):
        resolver.roles_for("alice@example.invalid")


def test_lazy_role_resolver_delegates_to_config_resolver(tmp_path):
    path = tmp_path / "roles.yaml"
    path.write_text("alice@example.invalid: [approver]\n")
    settings = _settings(review_role_source="config", review_role_assignments_path=str(path))
    resolver = LazyRoleResolver(settings)

    resolution = resolver.roles_for("alice@example.invalid")

    assert resolution.roles == frozenset({"approver"})
