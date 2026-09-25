"""P7 review workflow role resolution (docs/specs/P7_mapping_authoring_design.md
§3.5): resolves which of preparer/reviewer/approver roles an identity holds,
from one of two configured sources -- never both, never a fallback between
them (CLAUDE.md NN14). `orchestrator.signoff_policy` is the only caller; it
never talks to a `RoleResolver` implementation directly.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from orchestrator.config import Settings
from orchestrator.errors import ConfigError, RoleLookupFailed

ROLES: tuple[str, ...] = ("preparer", "reviewer", "approver")

# §3.5 "A 60 s in-process cache applies" (workspace_groups only -- a REST call
# per action click, never a warehouse query, so this bounds SDK round-trips
# without ever masking a group change for more than a minute).
_CACHE_TTL_S = 60.0


@dataclass(frozen=True)
class RoleResolution:
    roles: frozenset[str]
    # {role: matched_group} for workspace_groups (the group display name that
    # matched); {role: None} for config (there is no "group" -- §3.5's own
    # phrase "a config-derived role is never mistaken for a verified one" is
    # what role_source is for, not this field).
    matched_groups: dict[str, str | None] = field(default_factory=dict)
    role_source: str = "workspace_groups"


class RoleResolver(Protocol):
    def roles_for(self, email: str) -> RoleResolution: ...


class WorkspaceGroupsRoleResolver:
    """§3.5: `WorkspaceClient().users.list(filter='userName eq "<email>"',
    attributes="userName,groups")`, direct membership only (nested groups are
    not expanded). A lookup failure (network, permission, no such user)
    raises `RoleLookupFailed` -- it never falls back to the config resolver."""

    def __init__(self, settings: Settings, *, workspace_client_factory: Callable[[], object] | None = None):
        self.settings = settings
        self.workspace_client_factory = workspace_client_factory
        self._ws_client = None
        self._ws_client_lock = threading.Lock()
        self._cache: dict[str, tuple[float, RoleResolution]] = {}
        self._cache_lock = threading.Lock()

    def _workspace_client(self):
        # Mirrors DataSourceUC._workspace_client (orchestrator/adapters/datasource_uc.py):
        # cached per resolver instance, unified SDK auth resolution, never a
        # private `credentials_strategy` attribute.
        with self._ws_client_lock:
            if self._ws_client is None:
                if self.workspace_client_factory is not None:
                    self._ws_client = self.workspace_client_factory()
                else:
                    from databricks.sdk import WorkspaceClient

                    self._ws_client = WorkspaceClient(host=self.settings.host)
            return self._ws_client

    def roles_for(self, email: str) -> RoleResolution:
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(email)
            if cached is not None and now - cached[0] < _CACHE_TTL_S:
                return cached[1]

        try:
            client = self._workspace_client()
            users = list(
                client.users.list(filter=f'userName eq "{email}"', attributes="userName,groups")
            )
        except Exception as exc:
            raise RoleLookupFailed(email, str(exc)) from exc
        if not users:
            raise RoleLookupFailed(email, "no matching workspace user")

        user = users[0]
        group_names = {g.display for g in (user.groups or []) if getattr(g, "display", None)}
        configured = {
            "preparer": self.settings.review_preparer_groups,
            "reviewer": self.settings.review_reviewer_groups,
            "approver": self.settings.review_approver_groups,
        }
        roles: set[str] = set()
        matched: dict[str, str | None] = {}
        for role, groups in configured.items():
            hit = next((g for g in groups if g in group_names), None)
            if hit is not None:
                roles.add(role)
                matched[role] = hit

        resolution = RoleResolution(roles=frozenset(roles), matched_groups=matched, role_source="workspace_groups")
        with self._cache_lock:
            self._cache[email] = (now, resolution)
        return resolution


class ConfigRoleResolver:
    """§3.5 "config": a gitignored YAML, `{email: [role, ...]}` -- development
    and the local backend/e2e tests only. Read fresh on every call (test
    fixtures rewrite this file between cases; there is no TTL cache here,
    unlike the workspace_groups resolver, which is amortising a real network
    call)."""

    def __init__(self, path: str):
        self.path = path

    def roles_for(self, email: str) -> RoleResolution:
        import yaml

        try:
            with open(self.path) as f:
                data = yaml.safe_load(f) or {}
        except OSError as exc:
            raise RoleLookupFailed(email, f"cannot read {self.path!r}: {exc}") from exc
        if not isinstance(data, dict):
            raise RoleLookupFailed(email, f"{self.path!r} does not contain a mapping")

        raw_roles = data.get(email) or []
        if not isinstance(raw_roles, list):
            raise RoleLookupFailed(email, f"{self.path!r}: entry for {email!r} is not a list")
        roles = frozenset(r for r in raw_roles if r in ROLES)
        matched = {r: None for r in roles}
        return RoleResolution(roles=roles, matched_groups=matched, role_source="config")


def build_role_resolver(
    settings: Settings, *, workspace_client_factory: Callable[[], object] | None = None
) -> RoleResolver:
    source = settings.review_role_source
    if source == "config":
        if not settings.review_role_assignments_path:
            raise ConfigError("REVIEW_ROLE_ASSIGNMENTS is required when REVIEW_ROLE_SOURCE=config")
        return ConfigRoleResolver(settings.review_role_assignments_path)
    if source == "workspace_groups":
        # §3.6: "An unset group variable while in enforced mode is a
        # ConfigError ... There is no default group that silently admits
        # everyone." Checked here, at the point a resolver is actually built
        # for a review action, not eagerly at import time.
        if settings.review_sod_mode == "enforced":
            missing = [
                name
                for name, groups in (
                    ("REVIEW_PREPARER_GROUPS", settings.review_preparer_groups),
                    ("REVIEW_REVIEWER_GROUPS", settings.review_reviewer_groups),
                    ("REVIEW_APPROVER_GROUPS", settings.review_approver_groups),
                )
                if not groups
            ]
            if missing:
                raise ConfigError(
                    f"missing required configuration: {', '.join(missing)} "
                    f"(REVIEW_SOD_MODE=enforced requires every review group to be configured)"
                )
        return WorkspaceGroupsRoleResolver(settings, workspace_client_factory=workspace_client_factory)
    raise ConfigError(f"REVIEW_ROLE_SOURCE={source!r} is not one of 'workspace_groups', 'config'")
