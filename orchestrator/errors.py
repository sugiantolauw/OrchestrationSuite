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
