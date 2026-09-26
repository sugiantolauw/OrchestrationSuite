"""scripts.ensure_review_groups (independent review 2026-09-25 item 3):
importable, main(argv) accepts overrides and returns an int rather than
calling sys.exit directly (matching scripts/setup_workspace.py and
scripts/deploy_app.py's own convention). Every test here uses a fake
WorkspaceClient -- never a real workspace, and this module is never run by
any test, only imported and exercised through main()/ensure_group()
directly."""

from __future__ import annotations

import scripts.ensure_review_groups as ensure_review_groups


class _FakeGroup:
    def __init__(self, display_name, group_id, members=None):
        self.display_name = display_name
        self.id = group_id
        self.members = members or []


class _FakeComplexValue:
    def __init__(self, *, value=None, display=None):
        self.value = value
        self.display = display


class _FakeGroupsAPI:
    def __init__(self, groups=None):
        self._groups: dict[str, _FakeGroup] = {g.display_name: g for g in (groups or [])}
        self.create_calls: list[str] = []
        self.update_calls: list[tuple] = []

    def list(self):
        return list(self._groups.values())

    def create(self, *, display_name):
        group = _FakeGroup(display_name, f"grp-{len(self._groups) + 1}")
        self._groups[display_name] = group
        self.create_calls.append(display_name)
        return group

    def update(self, id, *, display_name=None, members=None):
        self.update_calls.append((id, display_name, members))
        for g in self._groups.values():
            if g.id == id:
                g.members = members
                return


class _FakeUser:
    def __init__(self, user_id, user_name):
        self.id = user_id
        self.user_name = user_name


class _FakeUsersAPI:
    def __init__(self, users=None):
        self._users = list(users or [])

    def list(self, *, filter):
        # Mirrors the real SCIM filter shape closely enough for these tests
        # (orchestrator/identity.py's own WorkspaceGroupsRoleResolver uses
        # the identical 'userName eq "<email>"' filter string).
        needle = filter.split('"')[1]
        return [u for u in self._users if u.user_name == needle]


class _FakeWorkspaceClient:
    def __init__(self, *, groups=None, users=None):
        self.groups = _FakeGroupsAPI(groups)
        self.users = _FakeUsersAPI(users)


def test_first_csv_name_takes_the_first_of_several():
    assert ensure_review_groups._first_csv_name("audit-preparers, other-group") == "audit-preparers"


def test_first_csv_name_none_when_unset():
    assert ensure_review_groups._first_csv_name(None) is None
    assert ensure_review_groups._first_csv_name("") is None


def test_ensure_group_creates_a_missing_group_and_adds_the_member():
    w = _FakeWorkspaceClient(users=[_FakeUser("u-1", "alice@example.com")])
    result = ensure_review_groups.ensure_group(w, "audit-preparers", "alice@example.com")
    assert "created group 'audit-preparers'" in result
    assert "added alice@example.com" in result
    assert w.groups.create_calls == ["audit-preparers"]
    ((group_id, display_name, members),) = w.groups.update_calls
    assert display_name == "audit-preparers"
    assert [m.value for m in members] == ["u-1"]
    assert [m.display for m in members] == ["alice@example.com"]


def test_ensure_group_is_a_noop_create_when_the_group_already_exists():
    existing = _FakeGroup("audit-preparers", "grp-9")
    w = _FakeWorkspaceClient(groups=[existing], users=[_FakeUser("u-1", "alice@example.com")])
    result = ensure_review_groups.ensure_group(w, "audit-preparers", "alice@example.com")
    assert "already exists" in result
    assert w.groups.create_calls == []
    assert len(w.groups.update_calls) == 1


def test_ensure_group_is_a_noop_when_already_a_member():
    existing = _FakeGroup(
        "audit-preparers", "grp-9", members=[_FakeComplexValue(value="u-1", display="alice@example.com")],
    )
    w = _FakeWorkspaceClient(groups=[existing], users=[_FakeUser("u-1", "alice@example.com")])
    result = ensure_review_groups.ensure_group(w, "audit-preparers", "alice@example.com")
    assert "already a member" in result
    assert w.groups.create_calls == []
    assert w.groups.update_calls == []


def test_ensure_group_raises_a_clear_error_for_an_unknown_user():
    w = _FakeWorkspaceClient(users=[])
    try:
        ensure_review_groups.ensure_group(w, "audit-preparers", "nobody@example.com")
        raised = False
    except SystemExit as exc:
        raised = True
        assert "nobody@example.com" in str(exc)
    assert raised


def test_main_reports_missing_group_configuration(monkeypatch, capsys):
    # Hermetic: the session may export real REVIEW_*_GROUPS, and this path must never reach a workspace.
    for name in ("REVIEW_PREPARER_GROUPS", "REVIEW_REVIEWER_GROUPS", "REVIEW_APPROVER_GROUPS"):
        monkeypatch.delenv(name, raising=False)
    import databricks.sdk

    def _no_workspace():
        raise AssertionError("test must not construct a real WorkspaceClient")

    monkeypatch.setattr(databricks.sdk, "WorkspaceClient", _no_workspace)
    rc = ensure_review_groups.main(["--member", "alice@example.com"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "preparer" in out
    assert "reviewer" in out
    assert "approver" in out


def test_main_never_calls_a_real_workspace_client(monkeypatch, capsys):
    """The exact fake-WorkspaceClient-driven happy path, wired through
    main() itself rather than only through ensure_group() directly --
    databricks.sdk.WorkspaceClient is patched BEFORE main() runs, so this
    never issues a real HTTP request to any workspace."""
    w = _FakeWorkspaceClient(users=[_FakeUser("u-1", "alice@example.com")])
    import databricks.sdk

    monkeypatch.setattr(databricks.sdk, "WorkspaceClient", lambda: w)
    rc = ensure_review_groups.main(
        ["--member", "alice@example.com", "--preparer-group", "audit-preparers",
         "--reviewer-group", "audit-reviewers", "--approver-group", "audit-approvers"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "preparer: created group 'audit-preparers'" in out
    assert "reviewer: created group 'audit-reviewers'" in out
    assert "approver: created group 'audit-approvers'" in out
    assert sorted(w.groups.create_calls) == ["audit-approvers", "audit-preparers", "audit-reviewers"]


def test_main_never_prints_a_token(monkeypatch, capsys):
    """Never prints a token, a secret, or anything from `.env` -- only group
    and user names, which this test's own env deliberately contaminates
    with a token-shaped value to prove it never leaks into output. Uses the
    same patched WorkspaceClient as the test above -- never a real one."""
    w = _FakeWorkspaceClient(users=[_FakeUser("u-1", "alice@example.com")])
    import databricks.sdk

    monkeypatch.setattr(databricks.sdk, "WorkspaceClient", lambda: w)
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-super-secret-value")
    rc = ensure_review_groups.main(
        ["--member", "alice@example.com", "--preparer-group", "audit-preparers",
         "--reviewer-group", "audit-reviewers", "--approver-group", "audit-approvers"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "dapi-super-secret-value" not in out
