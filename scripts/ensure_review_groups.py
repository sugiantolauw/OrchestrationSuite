"""Idempotently ensures the three REVIEW_*_GROUPS configured for the P7
review workflow (REVIEW_PREPARER_GROUPS / REVIEW_REVIEWER_GROUPS /
REVIEW_APPROVER_GROUPS, orchestrator/config.py) exist in this workspace, and
adds one member to every one of them.

Development-workspace convenience only. REVIEW_ROLE_SOURCE=workspace_groups
resolves every review action's role from real SCIM group membership
(orchestrator/identity.py's WorkspaceGroupsRoleResolver), and a fresh
workspace has no pre-existing "audit-preparers"/"audit-reviewers"/
"audit-approvers" groups to point it at. scripts/deploy_app.py's own
_check_review_groups_exist fails the deploy loudly instead of creating a
missing group -- this script is the explicit, separately-run step that
does, so a group is never created as a side effect of a deploy nobody asked
for that. Independent review 2026-09-25 item 3.

Never touches a workspace other than the one named in the sourced .env
(CLAUDE.md §10 "Do not connect to any workspace other than the one in the
environment variables"), and never prints a token, a secret, or anything
from the `.env` file itself -- only group and user names, which are not
secrets.

Usage:
    python scripts/ensure_review_groups.py --member someone@example.com
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the package root (parent of scripts/) -- a no-op (never
# raises) when the file does not exist, matching scripts/setup_workspace.py
# and scripts/deploy_app.py's own convention.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PACKAGE_ROOT / ".env")
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--member", required=True,
        help="The workspace user name (email) to add to every configured review group.",
    )
    parser.add_argument(
        "--preparer-group", default=None,
        help="Overrides REVIEW_PREPARER_GROUPS (the first comma-separated name, if several are configured).",
    )
    parser.add_argument("--reviewer-group", default=None, help="Overrides REVIEW_REVIEWER_GROUPS.")
    parser.add_argument("--approver-group", default=None, help="Overrides REVIEW_APPROVER_GROUPS.")
    return parser


def _first_csv_name(value: str | None) -> str | None:
    if not value:
        return None
    first = value.split(",")[0].strip()
    return first or None


def _find_user(w, user_name: str):
    users = list(w.users.list(filter=f'userName eq "{user_name}"'))
    if not users:
        raise SystemExit(f"No workspace user found with userName {user_name!r}.")
    return users[0]


def ensure_group(w, group_name: str, member_user_name: str) -> str:
    """Idempotent: finds an existing group by its exact display name,
    creates one if none exists, and adds `member_user_name` to it unless
    already a member. Returns a one-line, human-readable description of
    what happened -- never a token or a secret; a group name and a user
    name are not secrets."""
    from databricks.sdk.service.iam import ComplexValue

    existing = next((g for g in w.groups.list() if getattr(g, "display_name", None) == group_name), None)
    if existing is None:
        created = w.groups.create(display_name=group_name)
        group_id = created.id
        action = f"created group {group_name!r} ({group_id})"
        members = []
    else:
        group_id = existing.id
        action = f"group {group_name!r} already exists ({group_id})"
        members = list(existing.members or [])

    member_names = {m.display for m in members if getattr(m, "display", None)}
    if member_user_name in member_names:
        return f"{action}; {member_user_name} is already a member"

    user = _find_user(w, member_user_name)
    updated_members = members + [ComplexValue(value=user.id, display=member_user_name)]
    w.groups.update(id=group_id, display_name=group_name, members=updated_members)
    return f"{action}; added {member_user_name} as a member"


def main(argv: list[str] | None = None) -> int:
    """Importable entry point (same convention as scripts/deploy_app.py and
    scripts/setup_workspace.py -- runnable from a workspace notebook with no
    PAT). `argv` defaults to `sys.argv[1:]`; returns 0 on success, 1 on a
    resolvable-here configuration problem, never `sys.exit` directly for
    that case so a notebook caller can act on the return value."""
    args = build_parser().parse_args(argv)

    groups = {
        "preparer": args.preparer_group or _first_csv_name(os.environ.get("REVIEW_PREPARER_GROUPS")),
        "reviewer": args.reviewer_group or _first_csv_name(os.environ.get("REVIEW_REVIEWER_GROUPS")),
        "approver": args.approver_group or _first_csv_name(os.environ.get("REVIEW_APPROVER_GROUPS")),
    }
    missing = [role for role, name in groups.items() if not name]
    if missing:
        print(
            f"ERROR: no group name configured for: {', '.join(missing)} "
            f"(env REVIEW_*_GROUPS or --preparer-group/--reviewer-group/--approver-group)."
        )
        return 1

    try:
        from databricks.sdk import WorkspaceClient
    except ImportError:
        print("ERROR: databricks-sdk not installed. Run: pip install -r requirements.txt")
        return 1

    w = WorkspaceClient()
    for role, group_name in groups.items():
        result = ensure_group(w, group_name, args.member)
        print(f"{role}: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
