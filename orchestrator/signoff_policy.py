"""Segregation-of-duties policy for findings sign-off (CLAUDE.md §2.4; §11
"Approval decisions from the user (2026-09-23): accept all defaults, allow
self sign-off for now"; P7 review workflow, docs/specs/
P7_mapping_authoring_design.md §3).

Two generations of policy live here side by side:

- `evaluate_signoff` is the pre-P7 single-sign-off rule: self sign-off
  (approver == run_owner) is always allowed and simply labelled. It is what
  `orchestrator.runs.sign_off` calls today, and it stays correct for any run
  whose `signoff` was recorded before the P7 workflow existed (§3.7 "Existing
  runs").
- `evaluate_step`/`label_for`/`compute_sod_waived` are the P7 preparer ->
  reviewer -> approver policy (§3.3-3.7). `orchestrator.runs` (P7 WP B3b)
  calls these with data it has already loaded (the run's prior `review_steps`
  actors, its open note count, the actor's resolved roles) -- this module
  stays a pure decision function with no persistence or identity-lookup
  dependency of its own, so it is testable in isolation (§3.10 "Unit
  policy").
"""

from __future__ import annotations

from dataclasses import dataclass

SELF_APPROVED_LABEL = "Self-approved — segregation of duties not enforced"

# Retained for `orchestrator.service`'s pre-P7 call sites, removed there in
# WP B3b once `sign_off` gains the P7 stage gate (§3.7: "SOD_ENFORCED is
# removed, and the policy comes from config") -- self sign-off was never
# blocked by this flag, only labelled by it.
SOD_ENFORCED = False


def evaluate_signoff(*, actor: str, run_owner: str) -> dict:
    """Returns the `self_approved` / `sod_enforced` fields to record on a
    LEGACY `RunState.signoff` (no `prepared_by`/`reviewed_by`, §3.7) --
    never blocks the sign-off itself."""
    return {
        "self_approved": actor == run_owner,
        "sod_enforced": SOD_ENFORCED,
    }


# ── P7 review workflow (§3.3-3.7) ────────────────────────────────────────────

ROLES: tuple[str, ...] = ("preparer", "reviewer", "approver")
STAGES: tuple[str, ...] = ("preparation", "review", "approval")

# action -> (role required to take it, stage it must be taken in, stage it advances to)
_ACTIONS: dict[str, tuple[str, str, str | None]] = {
    "prepared": ("preparer", "preparation", "review"),
    "reviewed": ("reviewer", "review", "approval"),
    "approved": ("approver", "approval", None),
}


def role_for_action(action: str) -> str:
    return _ACTIONS[action][0]


def stage_for_action(action: str) -> str:
    return _ACTIONS[action][1]


def next_stage_for_action(action: str) -> str | None:
    return _ACTIONS[action][2]


@dataclass(frozen=True)
class StepDecision:
    allowed: bool
    role: str | None = None
    matched_group: str | None = None
    role_source: str | None = None
    reason: str | None = None


def evaluate_step(
    action: str,
    actor: str,
    *,
    stage: str,
    resolution,
    configured_groups: tuple[str, ...],
    prior_actors: dict[str, str],
    open_notes: int,
    sod_mode: str,
) -> StepDecision:
    """§3.3's action table, §3.10's "full role x stage x action matrix".

    `resolution` is an `orchestrator.identity.RoleResolution` (or anything
    with the same `.roles`/`.matched_groups`/`.role_source` shape).
    `configured_groups` names the groups configured for THIS action's role
    (§3.8 UI-R6's own refusal text names them). `prior_actors` is
    `{role: actor}` for every role already recorded against this run via
    `review_steps` (§3.4) -- `sod_mode='enforced'` refuses a second role for
    the same actor; `'labelled'` allows it (waived, recorded by the caller).
    """
    required_role, required_stage, _ = _ACTIONS[action]

    if stage != required_stage:
        return StepDecision(False, reason=(
            f"This run is at stage {stage!r} -- {action!r} is only valid at {required_stage!r}."
        ))
    if required_role not in resolution.roles:
        groups = ", ".join(configured_groups) if configured_groups else "none configured"
        return StepDecision(False, reason=(
            f"You are not in a preparer/reviewer/approver group ({groups})."
        ))
    if open_notes > 0:
        return StepDecision(False, reason=f"Clear every open review note first ({open_notes} open).")
    if sod_mode == "enforced":
        for role, prior_actor in prior_actors.items():
            if role != required_role and prior_actor == actor:
                return StepDecision(False, reason=(
                    f"Segregation of duties: you already acted on this run as {role}."
                ))

    return StepDecision(
        True,
        role=required_role,
        matched_group=resolution.matched_groups.get(required_role),
        role_source=resolution.role_source,
    )


def compute_sod_waived(prior_actors: dict[str, str]) -> list[list[str]]:
    """`prior_actors` is `{role: actor}` for every role recorded against a run
    (including the one just decided). Returns the role pairs that share an
    actor, sorted for determinism -- `RunState.signoff.sod_waived` (§3.4).
    Empty when every role was held by a different person."""
    roles = sorted(prior_actors)
    waived: list[list[str]] = []
    for i, role_a in enumerate(roles):
        for role_b in roles[i + 1 :]:
            if prior_actors[role_a] == prior_actors[role_b]:
                waived.append([role_a, role_b])
    return waived


def label_for(row: dict) -> str | None:
    """§3.7: derives the SAME `SELF_APPROVED_LABEL` text from either shape --
    a legacy `signoff` dict (`self_approved`, no `prepared_by`) or a P7
    `signoff`/`runs` row (`prepared_by`/`reviewed_by`/`approved_by`,
    `sod_waived`). `row.get("prepared_by")` being `None` is what
    distinguishes "no P7 workflow ran" from "P7 workflow ran, roles held by
    different people" -- the latter has `prepared_by` set and an empty
    `sod_waived`, and must NOT show the label."""
    if row.get("prepared_by") is None:
        return SELF_APPROVED_LABEL if row.get("self_approved") else None
    return SELF_APPROVED_LABEL if row.get("sod_waived") else None
