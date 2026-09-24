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
