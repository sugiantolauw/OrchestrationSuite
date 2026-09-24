from __future__ import annotations


class ConfigError(Exception):
    pass


class NotJsonSafe(Exception):
    pass


class InvalidTransition(Exception):
    def __init__(self, from_status: str, to_status: str, detail: str | None = None):
        self.from_status = from_status
        self.to_status = to_status
        msg = f"invalid transition {from_status!r} -> {to_status!r}"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


class StaleStateError(Exception):
    def __init__(self, run_id: str, expected_version: int, actual_version: int | None):
        self.run_id = run_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"stale state for run {run_id!r}: expected state_version={expected_version}, "
            f"actual={actual_version!r}"
        )


class RunNotFound(Exception):
    def __init__(self, run_id: str):
        self.run_id = run_id
        super().__init__(f"run not found: {run_id!r}")


class RunAlreadyExists(Exception):
    def __init__(self, run_id: str):
        self.run_id = run_id
        super().__init__(f"run already exists: {run_id!r}")


class FingerprintMismatch(Exception):
    def __init__(self, differing_fields: dict):
        self.differing_fields = differing_fields
        super().__init__(f"fingerprint mismatch on fields: {sorted(differing_fields)}")


class MigrationError(Exception):
    pass


class FingerprintConflict(Exception):
    def __init__(self, fingerprint_id: str, differing_fields: list[str]):
        self.fingerprint_id = fingerprint_id
        self.differing_fields = differing_fields
        super().__init__(
            f"fingerprint_id {fingerprint_id!r} already exists with different content "
            f"in fields: {differing_fields}"
        )


class NodeContractViolation(Exception):
    pass


class AttemptNotFound(Exception):
    def __init__(self, execution_key: str):
        self.execution_key = execution_key
        super().__init__(f"node attempt not found: {execution_key!r}")


class AttemptAlreadyClosed(Exception):
    def __init__(self, execution_key: str, existing_outcome: str, requested_outcome: str):
        self.execution_key = execution_key
        self.existing_outcome = existing_outcome
        self.requested_outcome = requested_outcome
        super().__init__(
            f"node attempt {execution_key!r} already closed with outcome "
            f"{existing_outcome!r}, cannot close again with {requested_outcome!r}"
        )


class EngagementNotFound(Exception):
    def __init__(self, engagement_id: str):
        self.engagement_id = engagement_id
        super().__init__(f"engagement not found: {engagement_id!r}")


class SkillVersionConflict(Exception):
    def __init__(self, skill_id: str, version: str, existing_hash: str, new_hash: str):
        self.skill_id = skill_id
        self.version = version
        self.existing_hash = existing_hash
        self.new_hash = new_hash
        super().__init__(
            f"skill_version ({skill_id!r}, {version!r}) already recorded with content_hash "
            f"{existing_hash!r}, cannot record different content_hash {new_hash!r}"
        )


class RiskStatusRegression(Exception):
    def __init__(self, risk_id: str, from_status: str, to_status: str):
        self.risk_id = risk_id
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"risk {risk_id!r}: status cannot move backwards from {from_status!r} to {to_status!r}"
        )


class FindingNotFound(Exception):
    def __init__(self, finding_id: str):
        self.finding_id = finding_id
        super().__init__(f"finding not found: {finding_id!r}")


class ManagementActionNotFound(Exception):
    """Independent review 2026-09-24 gap #3: raised by
    PersistenceAdapter.update_management_action when action_id names no
    persisted row -- never a silent no-op that would let an auditor's edit
    disappear without telling them."""

    def __init__(self, action_id: str):
        self.action_id = action_id
        super().__init__(f"management action not found: {action_id!r}")


class InvalidReviewStateTransition(Exception):
    def __init__(self, finding_id: str, from_state: str, to_state: str):
        self.finding_id = finding_id
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"finding {finding_id!r}: review_state cannot move from {from_state!r} to "
            f"{to_state!r} (only forward, one step at a time, along "
            f"draft -> prepared -> reviewed -> approved)"
        )


class ReconciliationError(Exception):
    """G6 (CLAUDE.md §5): tested-population rows/amount/min-max-date must
    reconcile to source totals with zero unexplained variance, or the run fails
    outright rather than proceeding on a number nobody can trust."""

    def __init__(self, run_id: str, differences: list[str]):
        self.run_id = run_id
        self.differences = list(differences)
        super().__init__(f"run {run_id!r}: reconciliation variance: {'; '.join(differences)}")


class LeaseNotHeld(Exception):
    def __init__(self, run_id: str, worker_id: str):
        self.run_id = run_id
        self.worker_id = worker_id
        super().__init__(f"run {run_id!r}: lease is not held by worker {worker_id!r}")


class NonDraftFindingWouldBeDeleted(Exception):
    def __init__(self, run_id: str, finding_ids: list[str]):
        self.run_id = run_id
        self.finding_ids = list(finding_ids)
        super().__init__(
            f"write_findings(run_id={run_id!r}): {sorted(finding_ids)} are no longer in the "
            f"new finding set but have review_state past 'draft' -- refusing to delete them"
        )


class RunNotReady(Exception):
    """Independent review 2026-09-24 item 5: start_audit_run refuses to
    start a run while a required readiness check (Volume, warehouse,
    configured source bindings, model endpoints) is failing. `failing_checks`
    names each one, sanitized (never a raw internal exception) -- the same
    discipline /ready itself uses."""

    def __init__(self, failing_checks: list[str]):
        self.failing_checks = list(failing_checks)
        super().__init__(
            "cannot start a run: the platform is not ready ({})".format(", ".join(self.failing_checks))
        )


class ConfiguredSourceUnavailable(Exception):
    """Independent review 2026-09-24 item 1: a contract source bound (via
    SOURCE_BINDINGS, orchestrator.source_bindings) to an exact Volume file
    that cannot be read -- missing, or a permission/transport failure.
    Raised instead of falling back to any bundled default: the corporate-
    workspace decision is explicit that per-diem rates and other Volume-
    bound sources must come from the Volume, with no fallback (CLAUDE.md
    NN14)."""

    def __init__(self, source: str, path: str, reason: str):
        self.source = source
        self.path = path
        self.reason = reason
        super().__init__(
            f"{source}: configured Volume file {path!r} could not be read ({reason}) -- "
            f"no bundled fallback; fix the SOURCE_BINDINGS entry or the file itself"
        )


class CeilingReached(Exception):
    """LIFECYCLE_design.md §2.6: a lifecycle run_kind's per-run Ceiling
    (max_llm_calls / max_total_tokens / other budgeted dimensions) was
    reached. Not a node failure: the node that catches this records
    module_output.stop_reason = f"ceiling:{which}" and marks its own
    coverage incomplete, then finishes cleanly -- there is never a silent
    partial result (CLAUDE.md §9A item 7)."""

    def __init__(self, which: str, used: int, limit: int):
        self.which = which
        self.used = used
        self.limit = limit
        super().__init__(f"ceiling reached: {which} used={used} limit={limit}")


class MonthlyLLMBudgetExceeded(Exception):
    """LIFECYCLE_design.md §2.6: LLM_MONTHLY_TOKEN_BUDGET is required to
    start any lifecycle run_kind that calls a model. Raised at admission
    (before the run does any model work) when the current UTC month's
    summed llm_calls.total_tokens plus this run_kind's own ceiling would
    exceed the configured budget -- refused with a reason, never a silent
    partial run."""

    def __init__(self, *, used_tokens: int, requested_tokens: int, budget: int):
        self.used_tokens = used_tokens
        self.requested_tokens = requested_tokens
        self.budget = budget
        super().__init__(
            f"starting this run would exceed the monthly LLM token budget: "
            f"used={used_tokens} + requested={requested_tokens} > budget={budget}"
        )


class ExplorerInputError(Exception):
    """docs/specs/P6_P8_explorer_llm_design.md §4.2: start_explorer_run was
    given inputs it cannot honour -- wrong number of sources, an unset
    audit_timezone, an objective outside its length bounds, or a rendered
    planner prompt over explorer_max_prompt_chars (§4.4's size guard,
    raised loudly before any model call, never truncated)."""


class ExplorerEditRejected(Exception):
    """docs/specs/P6_P8_explorer_llm_design.md §4.10: a submitted batch of
    plan_edits would leave an included test invalid. The whole batch is
    refused -- nothing partial is ever recorded."""

    def __init__(self, reasons: list[str]):
        self.reasons = list(reasons)
        super().__init__(f"edit rejected: {'; '.join(self.reasons)}")


class ExplorerPlanNotConfirmable(Exception):
    """docs/specs/P6_P8_explorer_llm_design.md §4.10 confirm_plan: the plan
    is not in a confirmable state -- not awaiting_confirmation, not
    Explorer mode, plan.status != 'proposed' (e.g. llm_unavailable or
    no_valid_tests), or no included test remains valid after edits."""


class PlanIntegrityError(Exception):
    """docs/specs/P6_P8_explorer_llm_design.md §4.11 resolve_run_skill: an
    Explorer run's confirmed_plan_hash does not match its EXPLORER-<run_id>
    ledger row's content_hash (or that row is missing) -- the SAME handling
    as a fingerprint mismatch (CLAUDE.md §3 non-negotiable 8): the executor
    pass fails the run rather than executing against a Skill nobody
    confirmed."""


class PromotionRequirementsNotMet(Exception):
    """docs/specs/P6_P8_explorer_llm_design.md §4.13: publish_skill's guard
    -- Surface 2 results, a named reviewer distinct from created_by, and a
    'draft' status are all required before a Skill (repo or Explorer) may
    move to 'published'. No publish UI exists yet (§4.13); this is the
    guard alone."""

    def __init__(self, missing: list[str]):
        self.missing = list(missing)
        super().__init__(f"cannot publish: requirements not met: {', '.join(self.missing)}")


class RunNotAwaitingSignoff(Exception):
    """P6 WP N9 (docs/specs/P6_narration_design.md §5.2, §5.5, §6.4): a
    candidate decision, a narrative edit or a regenerate request was made
    against a run that is not currently `awaiting_signoff` -- the only
    status any of the three is meaningful against (before it, there is
    nothing to decide or edit yet; after it, sign-off has already frozen
    the narration for export)."""

    def __init__(self, run_id: str, status: str):
        self.run_id = run_id
        self.status = status
        super().__init__(
            f"run {run_id!r} is {status!r}, not 'awaiting_signoff' -- this action is only valid then"
        )


class CandidateNotFound(Exception):
    def __init__(self, candidate_id: str):
        self.candidate_id = candidate_id
        super().__init__(f"AI-proposed finding candidate not found: {candidate_id!r}")


class CandidateAlreadyDecided(Exception):
    """§5.2: the conditional `decide_candidate_cas` UPDATE affected zero
    rows because the candidate was already accepted or rejected -- by this
    same decide_candidate call racing another, or by an earlier one."""

    def __init__(self, candidate_id: str, status: str):
        self.candidate_id = candidate_id
        self.status = status
        super().__init__(f"candidate {candidate_id!r} was already decided (status={status!r})")


class CandidateSuperseded(Exception):
    """§5.2: the conditional `decide_candidate_cas` UPDATE affected zero
    rows because a racing `regenerate_narration` superseded this candidate
    first (T-C6) -- a later generation's candidates replace it."""

    def __init__(self, candidate_id: str):
        self.candidate_id = candidate_id
        super().__init__(
            f"candidate {candidate_id!r} was superseded by a narration regeneration before it "
            f"could be decided"
        )


class CandidateSeverityRequired(Exception):
    """§5.2/§14 Answers Q4: accepting an AI-proposed finding requires the
    auditor's own severity choice -- the model's `proposed_severity` is
    shown, never applied silently."""

    def __init__(self, candidate_id: str):
        self.candidate_id = candidate_id
        super().__init__(f"candidate {candidate_id!r}: decided_severity is required to accept")


class CandidateReasonRequired(Exception):
    """§5.2: "reason is required for a reject" -- a rejected candidate is
    kept, never deleted (CLAUDE.md §3 NN2 amendment), and the reason is
    what makes that record meaningful."""

    def __init__(self, candidate_id: str):
        self.candidate_id = candidate_id
        super().__init__(f"candidate {candidate_id!r}: a reason is required to reject")


class CandidatesUndecided(Exception):
    """§5.2: "Sign-off is refused while any current-generation row is
    candidate" -- the UI's own message (P6_narration_design.md §7 UI-3) is
    reproduced verbatim in this exception so the service layer never has to
    re-word it."""

    def __init__(self, run_id: str, candidate_ids: list[str]):
        self.run_id = run_id
        self.candidate_ids = list(candidate_ids)
        super().__init__("decide every AI-proposed finding before sign-off")


class NarrativeNotFound(Exception):
    def __init__(self, narrative_id: str):
        self.narrative_id = narrative_id
        super().__init__(f"narrative not found: {narrative_id!r}")


class NarrativeTargetNotFound(Exception):
    """WP N9's own `edit_narrative`: the finding/candidate/theme/chart a
    narrative row points at (`target_kind`/`target_id`) no longer resolves
    against this run's persisted findings/candidates/themes -- there is no
    placeholder table to validate a human edit against."""

    def __init__(self, narrative_id: str, target_kind: str, target_id: str):
        self.narrative_id = narrative_id
        self.target_kind = target_kind
        self.target_id = target_id
        super().__init__(
            f"narrative {narrative_id!r}: its target ({target_kind}={target_id!r}) was not found "
            f"in this run's current findings/candidates/themes"
        )


class NarrativeEditNotAllowed(Exception):
    """§6.4: "allowed only while awaiting_signoff ... After sign-off, no
    edits are possible" -- raised for either side of that window."""

    def __init__(self, narrative_id: str, status: str):
        self.narrative_id = narrative_id
        self.status = status
        super().__init__(
            f"narrative {narrative_id!r}: edits are only allowed while the run is "
            f"'awaiting_signoff' (current status: {status!r})"
        )


class NarrativeEditRejected(Exception):
    """CLAUDE.md §14 Answers Q3: "If a number doesn't match, the edit is
    refused with a message naming the mismatched number." `violations` is
    every `orchestrator.narration.validate.Violation` the edit failed,
    serialised as `{rule_id, message, text}` -- the N-H1 violation's own
    message already names the exact mismatched substring."""

    def __init__(self, narrative_id: str, violations: list[dict]):
        self.narrative_id = narrative_id
        self.violations = list(violations)
        detail = "; ".join(f"{v['rule_id']}: {v['message']}" for v in self.violations)
        super().__init__(f"narrative {narrative_id!r}: edit rejected -- {detail}")


class NarrativeEditConflict(Exception):
    """P6 WP N10 (docs/specs/P6_narration_design.md §6.1's persistence
    contract, `upsert_narrative(row, expected_version=...)`): a real CAS on
    `narratives.version` -- `edit_narrative` read the row at one version and
    the conditional write found a different version already stored (another
    edit, or a `narrate()` re-execution, landed first). Refused, never
    silently overwritten (CLAUDE.md NN14); the caller re-reads the current
    narrative and retries with the new expected version."""

    def __init__(self, narrative_id: str, expected_version: int, actual_version: int | None):
        self.narrative_id = narrative_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"narrative {narrative_id!r}: expected version {expected_version}, "
            f"found {actual_version!r} -- edit refused, re-read and retry"
        )


class NarrativeVersionMismatch(Exception):
    """P6 WP N10 (§6.4's `versions=` argument to `effective_prose`): an
    export/resolver read asked for a specific signed-off `narratives.version`
    (`RunState.signoff.narration.narrative_versions`) and the CURRENTLY
    stored row is a different version. Because edits and regeneration are
    both blocked once a run leaves `awaiting_signoff` (§6.4: "After sign-off,
    no edits are possible"), this should never actually happen against a
    genuinely frozen run -- surfaced as a loud failure rather than silently
    rendering a version nobody signed off on (CLAUDE.md NN14)."""

    def __init__(self, narrative_id: str, expected_version: int, actual_version: int):
        self.narrative_id = narrative_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"narrative {narrative_id!r}: the signed-off version is {expected_version}, "
            f"but the stored row is version {actual_version!r} -- refusing to render a "
            f"version nobody signed off on"
        )


class NarrationDisabled(Exception):
    def __init__(self, run_id: str):
        self.run_id = run_id
        super().__init__(f"run {run_id!r}: narration is disabled (NARRATION_ENABLED is off)")


class NarrationNodeUnavailable(Exception):
    """A `regenerate_narration` precondition that has nothing to do with
    the auditor's request: this run_kind/phase's node sequence (CLAUDE.md
    §4.2) has no `narrate` node to restart at. Distinct from
    `NarrationDisabled` so a genuine configuration/deployment gap is never
    reported to an auditor as if they had simply left narration off."""

    def __init__(self, run_id: str, run_kind: str, phase: str):
        self.run_id = run_id
        self.run_kind = run_kind
        self.phase = phase
        super().__init__(
            f"run {run_id!r}: no 'narrate' node is scheduled for run_kind={run_kind!r} "
            f"phase={phase!r} -- narration regeneration is unavailable"
        )


class MissingSeverityProvenance(Exception):
    """CLAUDE.md §0.4/G8, P2/P3 gate review item 3: a finding whose severity
    provenance (analyst_set_severity / severity_basis) was never persisted.
    Raised by every reader (XLSX export, UI) instead of silently defaulting
    to False -- an unattributed severity is not defensible in a CAO meeting."""

    def __init__(self, finding_id: str, field: str):
        self.finding_id = finding_id
        self.field = field
        super().__init__(
            f"finding {finding_id!r}: {field} was not persisted -- a severity's provenance "
            f"must never be assumed (CLAUDE.md §0.4)"
        )
