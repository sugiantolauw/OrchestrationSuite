"""Segregation-of-duties policy for findings sign-off (CLAUDE.md §2.4; §11
"Approval decisions from the user (2026-09-23): accept all defaults, allow
self sign-off for now").

Self sign-off (the approver being the same identity as `RunState.run_owner`)
is allowed until P7, which replaces this with an enforced
preparer/reviewer/approver rule. This module is the single place that decides
whether a given sign-off counts as self-approved and whether SoD is enforced,
so P7 can flip `SOD_ENFORCED` (or make the rule data-driven off Skill/engagement
config) without touching every call site that reads `RunState.signoff`.
"""

from __future__ import annotations

# Flipped in P7 when the enforced preparer/reviewer/approver rule lands
# (CLAUDE.md §2.4, §9C "Authorization model"). Until then, sign-off is never
# blocked on identity -- it is only ever labelled.
SOD_ENFORCED = False

SELF_APPROVED_LABEL = "Self-approved — segregation of duties not enforced"


def evaluate_signoff(*, actor: str, run_owner: str) -> dict:
    """Returns the `self_approved` / `sod_enforced` fields to record on
    `RunState.signoff` for this sign-off. Never blocks the sign-off itself --
    SoD enforcement is a P7 feature; this only decides how a self-approved
    sign-off is labelled everywhere it is later displayed or exported."""
    return {
        "self_approved": actor == run_owner,
        "sod_enforced": SOD_ENFORCED,
    }
