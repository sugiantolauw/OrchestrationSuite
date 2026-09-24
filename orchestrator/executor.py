"""ThreadExecutor (CLAUDE.md §2.3): the default, in-App executor. A background
admission thread polls `persistence.find_runs(['queued'])`, claims a lease per
run (CLAUDE.md §9C -- leases are the P3 extension of the P1A CAS/idempotent-
MERGE foundation to multi-worker safety), and submits `orchestrator.pipeline.
run_phase` to a bounded thread pool (`Semaphore(MAX_CONCURRENT_RUNS)`); a
heartbeat thread renews every lease this worker holds so a live run is never
mistaken for orphaned mid-flight. Runs beyond the cap simply stay `queued` in
Delta -- no in-memory queue, Delta is the queue (CLAUDE.md §2.3 rule 3).

Nothing here is specific to the `fieldwork` run_kind or to Dash: this module
knows the pipeline loop and the lease/admission protocol, nothing else
(CLAUDE.md §2.5 -- "no node may reference the executor... the executor never
renders UI").

Two P3 gate-review hardening items live here too: bounded, backed-off
admission retries that end an unadmittable run `failed` through the normal
state machine rather than polling forever (§3a); and a lease renewal failure
marking this worker `not alive` for that run so the pipeline stops writing
further node output (§3b, consulted by orchestrator.pipeline.run_phase's own
worker_alive() check).

A third: queue environment affinity, which never even attempts a lease for a
run created under a different deployment's code_revision/runtime_config_hash,
leaving it queued for the deployment it actually belongs to (§4)."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Callable

from orchestrator.adapters.protocols import NullTracing
from orchestrator.config import runtime_config_hash
from orchestrator.errors import InvalidTransition, StaleStateError
from orchestrator.nodes.fieldwork import NODES_FOR
from orchestrator.pipeline import run_phase
from orchestrator.reaper import _trace_event_id
from orchestrator.status import transition
from orchestrator.timeutil import normalise_ts

logger = logging.getLogger(__name__)

_DEFAULT_LEASE_TTL_S = 90.0
_DEFAULT_HEARTBEAT_INTERVAL_S = 20.0
_DEFAULT_POLL_INTERVAL_S = 2.0

# Any finite lease_expires_at is <= this, so expired_leases() at this
# timestamp returns every run_id currently holding ANY lease row -- live or
# expired. Used only to distinguish "never leased" from "leased and live"
# below; never passed anywhere as an actual clock value.
_FAR_FUTURE = "9999-12-31T23:59:59.999999Z"


def _add_seconds(ts: str, seconds: float) -> str:
    from datetime import datetime as _dt

    return normalise_ts(_dt.fromisoformat(ts.replace("Z", "+00:00")) + timedelta(seconds=seconds))


def reap_orphaned_runs_with_leases(persistence, *, now: str, actor: str = "reaper") -> list[str]:
    """App-start reaping (CLAUDE.md §2.3 rule 2), lease-aware: a run left
    `running` with NO live lease -- never leased at all (a pre-P3-shaped
    orphan: created but the executor died before admission claimed a lease),
    or its lease expired -- was orphaned by a container restart/crash and is
    marked `interrupted`. A run whose lease is still live is left alone: some
    worker is actively holding it, so it is not orphaned.

    This reimplements orchestrator.reaper.reap_orphaned_runs's per-run body
    (same CAS-then-close-attempts ordering, same trace event shape) rather
    than importing it, because that function reaps every `running` run
    unconditionally and CLAUDE.md build brief P3 §1 is explicit: extend the
    call site, never reaper.py's semantics."""
    running = set(persistence.find_runs(["running"]))
    if not running:
        return []
    ever_leased = set(persistence.expired_leases(_FAR_FUTURE)) & running
    expired = set(persistence.expired_leases(now)) & running
    never_leased = running - ever_leased
    eligible = expired | never_leased
    if not eligible:
        return []

    reaped: list[str] = []
    for run_id in sorted(eligible):
        reason = f"Executor not alive at App start (lease absent or expired); run orphaned at {now}"
        state = persistence.load_state(run_id)
        if state.status != "running":
            continue
        new_state = transition(state, "interrupted", now=now, reason=reason)
        try:
            saved = persistence.save_state(new_state)
        except StaleStateError:
            continue
        persistence.close_open_attempts(run_id, outcome="interrupted", now=now, error_detail=reason)
        persistence.append_trace_event(
            {
                "event_id": _trace_event_id(run_id, "run_interrupted", saved.state_version),
                "run_id": run_id,
                "engagement_id": saved.engagement_id,
                "event_type": "run_interrupted",
                "event_time": now,
                "stage": "Recovery",
                "status": saved.status,
                "message": reason,
                "duration_s": None,
                "node_name": None,
                "execution_key": None,
                "actor": actor,
                "state_version": saved.state_version,
            }
        )
        reaped.append(run_id)
    return reaped


class ThreadExecutor:
    def __init__(
        self,
        *,
        persistence,
        settings,
        worker_id: str,
        ctx_factory: Callable[[str], object],
        fingerprint_factory: Callable[[str], dict],
        clock: Callable[[], str],
        nodes_for: dict | None = None,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
        idle_poll_interval_s: float | None = None,
        lease_ttl_s: float = _DEFAULT_LEASE_TTL_S,
        heartbeat_interval_s: float = _DEFAULT_HEARTBEAT_INTERVAL_S,
        tracing=None,
    ):
        self._persistence = persistence
        self._settings = settings
        self._worker_id = worker_id
        self._ctx_factory = ctx_factory
        self._fingerprint_factory = fingerprint_factory
        self._clock = clock
        self._nodes_for = nodes_for or NODES_FOR
        self._tracing = tracing or NullTracing()
        # "Active" cadence (poll_interval_s) applies while this worker has any
        # run in flight or is backing off a lease retry; "idle" is an OPT-IN
        # periodic safety sweep the rest of the time, for a multi-container
        # deployment where a run admitted elsewhere must still be noticed by
        # a worker that received no direct wake for it. idle_poll_interval_s
        # <= 0 (the default -- CLAUDE.md P3 cost fix) disables that sweep
        # entirely: the loop still reaps + admits once, immediately, on every
        # start() (nothing queued or orphaned at App start is ever missed),
        # then BLOCKS on `_wake_event` -- no timer, no persistence call --
        # until stop() or a direct start(run_id, phase) call (the same one
        # start_audit_run/confirm_plan/sign_off/resume_run already make to
        # admit that run itself) nudges it. Defaulting idle_poll_interval_s to
        # poll_interval_s when not given keeps every existing caller (tests
        # that pass only poll_interval_s) at its prior constant cadence;
        # production wiring (orchestrator/service.py) passes both explicitly
        # from Settings.
        self._poll_interval_s = poll_interval_s
        self._idle_poll_interval_s = (
            idle_poll_interval_s if idle_poll_interval_s is not None else poll_interval_s
        )
        self._lease_ttl_s = lease_ttl_s
        self._heartbeat_interval_s = heartbeat_interval_s
        # Set by start(run_id, ...) after every direct admission attempt, and
        # by stop() for a clean shutdown -- the only two things that wake a
        # loop currently blocked with the idle sweep disabled. Checking (and
        # clearing) it is free; it never itself triggers a persistence call.
        self._wake_event = threading.Event()

        max_workers = max(1, int(getattr(settings, "max_concurrent_runs", 2)))
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="run-exec")
        self._sema = threading.Semaphore(max_workers)
        self._lock = threading.Lock()
        self._active_runs: set[str] = set()
        self._stop_event = threading.Event()
        self._admission_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None

        # Bounded admission retries (CLAUDE.md §2.3 rule 3 / §9C, P3 gate review
        # item 3a): {run_id: {"attempts": int, "next_retry_at": ISO str}} for a
        # queued run whose lease repeatedly cannot be acquired, so it backs off
        # between retries instead of hammering every poll tick, and eventually
        # ends `failed` through the normal state machine (§3a below) instead of
        # retrying forever. Guarded by self._lock along with _active_runs.
        self._admission_max_attempts = max(1, int(getattr(settings, "admission_max_attempts", 20)))
        self._admission_backoff_base_s = float(getattr(settings, "admission_backoff_base_s", 2.0))
        self._admission_backoff_max_s = float(getattr(settings, "admission_backoff_max_s", 60.0))
        self._admission_failures: dict[str, dict] = {}

        # A run whose lease renewal failed (§3b): the pipeline's own worker_alive()
        # check (orchestrator/pipeline.py) consults this so no further node output
        # is written once the lease is lost, without waiting for this worker's task
        # to notice on its own. Cleared in _on_done so a run_id can be retried clean
        # if it is ever re-admitted by this worker.
        self._lease_lost: set[str] = set()

        # Queue environment affinity (P3 gate review item 4): this worker's own
        # deployment identity, computed once from `settings` -- the same two
        # fields a queued run's stored run_fingerprints row carries
        # (orchestrator/fingerprint.py's _HASHED_FIELDS). A run admitted by a
        # DIFFERENT deployment would otherwise be claimed here and spend a
        # lease only to fail at pipeline.run_phase's own fingerprint
        # verification -- this skips it at admission instead, leaving it
        # `queued` for the deployment that actually matches. `runtime_config_hash`
        # requires a real `Settings` dataclass; test doubles that are not one
        # (or that never set `code_revision`) fall back to "unknown", under
        # which the gate never blocks admission -- CLAUDE.md never assumes a
        # missing value silently means "matches" for anything that DOES fail
        # closed, but here the absence is "this worker cannot tell", and the
        # existing full fingerprint check inside run_phase remains the backstop.
        own_code_revision = getattr(settings, "code_revision", None)
        try:
            own_runtime_config_hash = runtime_config_hash(settings)
        except Exception:
            own_runtime_config_hash = None
        self._own_environment: dict[str, str | None] = {
            "code_revision": own_code_revision,
            "runtime_config_hash": own_runtime_config_hash,
        }

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self, run_id: str | None = None, phase: str | None = None) -> None:
        """Executor Protocol conformance (`start(run_id, phase)`) AND the
        background-loop starter, unified: called with no arguments (App start),
        it starts the admission + heartbeat threads. Called with a run_id (a
        `start_audit_run` caller wanting immediacy rather than waiting for the
        next poll tick), it attempts to admit that one run right away -- a
        no-op if the loop is not running yet or the run is not admissible."""
        if run_id is not None:
            self._try_admit(run_id)
            # Nudge the admission loop in case it is currently blocked on the
            # idle wake (idle_poll_interval_s disabled, CLAUDE.md P3 cost
            # fix): this run was likely already handled by the direct
            # _try_admit above, but a queued run this worker was not woken
            # for (e.g. one that freed up capacity for) may also be waiting,
            # and this is a real state-changing event, not periodic polling.
            self._wake_event.set()
            return
        if self._admission_thread is not None and self._admission_thread.is_alive():
            return
        self._stop_event.clear()
        self._admission_thread = threading.Thread(
            target=self._admission_loop, name="executor-admission", daemon=True
        )
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="executor-heartbeat", daemon=True
        )
        self._admission_thread.start()
        self._heartbeat_thread.start()

    def stop(self, *, wait: bool = True) -> None:
        self._stop_event.set()
        self._wake_event.set()  # unblock the loop if it is parked on the idle wake
        if self._admission_thread is not None:
            self._admission_thread.join(timeout=5)
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=5)
        self._pool.shutdown(wait=wait)

    def worker_alive(self, run_id: str) -> bool:
        with self._lock:
            if run_id in self._lease_lost:
                return False
        return not self._stop_event.is_set() and run_id in self._active_runs

    # ── admission ────────────────────────────────────────────────────────────

    def _admission_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._reap_orphans()
                self._admit_all_queued()
            except Exception:  # pragma: no cover - defensive, the loop must not die
                logger.exception("admission loop error")
            # A single wait, in BOTH the active and idle cases: `_wake_event`
            # is set by every real in-process signal -- start(run_id, phase)
            # (start_audit_run/confirm_plan/sign_off/resume_run), _on_done
            # (a run finishing, possibly freeing the slot a queued run is
            # waiting on -- CLAUDE.md P3 gate review: "make sure _on_done
            # still admits queued runs after the last active run finishes"),
            # and stop(). Event.wait(None) blocks indefinitely, which is
            # exactly "idle sweep disabled": no timer, no persistence call,
            # until one of those signals fires. Event.wait(interval) is the
            # active/opt-in-idle cadence, but now ALSO wakes early on a real
            # signal rather than always sleeping out the full interval --
            # strictly lower latency, never higher, than waiting on
            # `_stop_event` alone did.
            interval = self._next_poll_interval()
            self._wake_event.wait(timeout=interval)
            self._wake_event.clear()

    def _next_poll_interval(self) -> float | None:
        """Fast cadence while this worker has anything in flight (a run it is
        actively executing, or a queued run backing off a failed lease
        acquisition); otherwise the OPT-IN idle safety-sweep interval, or
        None when that sweep is disabled (idle_poll_interval_s <= 0, the
        default) -- meaning "block on _wake_event, do not poll on a timer at
        all". A new run created through the normal service functions is
        picked up immediately regardless -- they call executor.start(run_id,
        phase), which admits directly -- so this only controls how long an
        orphan or a queued run this worker was not directly woken for can sit
        before the next sweep (if any) notices it."""
        with self._lock:
            busy = bool(self._active_runs) or bool(self._admission_failures)
        if busy:
            return self._poll_interval_s
        if self._idle_poll_interval_s <= 0:
            return None
        return self._idle_poll_interval_s

    def _reap_orphans(self) -> None:
        # CLAUDE.md §2.3 rule 2 / P2/P3 gate review item 8: App-start reaping
        # alone leaves a run orphaned WITHOUT a restart permanently `running`
        # -- a live run whose worker died (or a lease that simply expired for
        # any other reason) between App starts was never caught (found live:
        # a run sat `running` for ~58 minutes with nothing marking it
        # `interrupted` or offering Resume). Every admission tick also reaps,
        # using the exact same lease-aware semantics as App start (never
        # delete, never auto-resume -- reap_orphaned_runs_with_leases only
        # ever moves a run to `interrupted`) -- cheap when there is nothing
        # to reap (one query, `find_runs(["running"])`, short-circuits to a
        # no-op), so running it every poll tick is not a meaningful cost.
        try:
            reaped = reap_orphaned_runs_with_leases(self._persistence, now=self._clock())
        except Exception:  # pragma: no cover - defensive
            logger.exception("reap_orphaned_runs_with_leases failed")
            return
        if reaped:
            logger.info("admission loop reaped orphaned run(s): %s", sorted(reaped))

    def _admit_all_queued(self) -> None:
        for run_id in self._persistence.find_runs(["queued"]):
            self._try_admit(run_id)

    def _try_admit(self, run_id: str) -> None:
        now = self._clock()
        with self._lock:
            if run_id in self._active_runs:
                return
            info = self._admission_failures.get(run_id)
            if info is not None and now < info["next_retry_at"]:
                return  # backing off after a prior failure -- not due for retry yet
        if self._has_own_environment_signature() and not self._fingerprint_environment_matches(run_id):
            # A different deployment's run (P3 gate review item 4) -- never touched:
            # no lease attempt, no backoff bookkeeping, stays `queued` untouched for
            # the deployment it actually belongs to. Not a failure of THIS run.
            return
        if not self._sema.acquire(blocking=False):
            return  # at capacity -- stays `queued` in Delta (CLAUDE.md §2.3 rule 3)

        acquired = False
        try:
            acquired = self._persistence.acquire_lease(run_id, self._worker_id, ttl_s=self._lease_ttl_s, now=now)
        except Exception:  # pragma: no cover - defensive
            logger.exception("acquire_lease failed for run_id=%s", run_id)
        if not acquired:
            self._sema.release()
            self._record_admission_failure(run_id, now)
            return

        self._clear_admission_failure(run_id)
        with self._lock:
            self._active_runs.add(run_id)
        future = self._pool.submit(self._run_one, run_id)
        future.add_done_callback(lambda f, rid=run_id: self._on_done(rid, f))

    def _has_own_environment_signature(self) -> bool:
        return any(v is not None for v in self._own_environment.values())

    def _fingerprint_environment_matches(self, run_id: str) -> bool:
        """P3 gate review item 4: compares only `code_revision` and
        `runtime_config_hash` against the run's STORED run_fingerprints row --
        never the full fingerprint (source table versions, skill content
        hash, ...), which is per-run by design and belongs to
        pipeline.run_phase's own verify_fingerprint call, not an admission
        gate every queued run would otherwise pay for on every poll tick."""
        try:
            state = self._persistence.load_state(run_id)
            stored_fingerprint = self._persistence.get_fingerprint(state.fingerprint_id)
        except Exception:  # pragma: no cover - defensive
            logger.exception("environment-affinity fingerprint lookup failed for run_id=%s", run_id)
            return True  # fail open -- never block admission on a lookup error
        for field, own_value in self._own_environment.items():
            if own_value is None:
                continue
            stored_value = (stored_fingerprint or {}).get(field)
            if stored_value is not None and stored_value != own_value:
                return False
        return True

    def _record_admission_failure(self, run_id: str, now: str) -> None:
        """CLAUDE.md §2.3 rule 3 / §9C, P3 gate review item 3a: repeated
        admission/lease failures for a queued run must not retry every poll
        tick forever. Backs off exponentially (capped) between retries, and
        on exhaustion fails the run through the normal state machine/CAS
        path -- never a direct UPDATE bypassing CAS."""
        with self._lock:
            attempts = self._admission_failures.get(run_id, {"attempts": 0})["attempts"] + 1
            if attempts >= self._admission_max_attempts:
                self._admission_failures.pop(run_id, None)
                exhausted = True
            else:
                backoff = min(
                    self._admission_backoff_base_s * (2 ** (attempts - 1)),
                    self._admission_backoff_max_s,
                )
                self._admission_failures[run_id] = {
                    "attempts": attempts,
                    "next_retry_at": _add_seconds(now, backoff),
                }
                exhausted = False
        if exhausted:
            self._fail_admission_exhausted(run_id, now=now, attempts=attempts)

    def _clear_admission_failure(self, run_id: str) -> None:
        with self._lock:
            self._admission_failures.pop(run_id, None)

    def _fail_admission_exhausted(self, run_id: str, *, now: str, attempts: int) -> None:
        reason = (
            f"admission failed after {attempts} attempts (lease could not be acquired) -- "
            "ending the run rather than retrying indefinitely"
        )
        try:
            state = self._persistence.load_state(run_id)
        except Exception:  # pragma: no cover - defensive
            logger.exception("load_state failed while failing exhausted admission for run_id=%s", run_id)
            return
        if state.status != "queued":
            return  # already moved on (admitted by another worker, resumed, etc.) -- nothing to fail
        try:
            failed_state = transition(state, "failed", now=now, reason=reason)
            saved = self._persistence.save_state(failed_state)
        except StaleStateError:
            return
        except InvalidTransition:  # pragma: no cover - defensive
            logger.exception("invalid transition failing exhausted admission for run_id=%s", run_id)
            return
        self._persistence.close_open_attempts(run_id, outcome="failed", now=now, error_detail=reason)
        self._persistence.append_trace_event(
            {
                "event_id": _trace_event_id(run_id, "run_admission_failed", saved.state_version),
                "run_id": run_id,
                "engagement_id": saved.engagement_id,
                "event_type": "run_admission_failed",
                "event_time": now,
                "stage": saved.phase,
                "status": saved.status,
                "message": reason,
                "duration_s": None,
                "node_name": None,
                "execution_key": None,
                "actor": self._worker_id,
                "state_version": saved.state_version,
            }
        )
        logger.error("run_id=%s: admission exhausted after %d attempts, marked failed", run_id, attempts)

    def _run_one(self, run_id: str) -> None:
        try:
            ctx = self._ctx_factory(run_id)
            fingerprint = self._fingerprint_factory(run_id)
            run_phase(
                self._persistence,
                run_id,
                nodes_for=self._nodes_for,
                skill=ctx,
                clock=self._clock,
                worker_alive=lambda: self.worker_alive(run_id),
                current_fingerprint=fingerprint,
                tracing=self._tracing,
            )
        except StaleStateError:
            # Another worker (or a resume racing this one) already moved the
            # run on -- a normal admission skip, not a failure (CLAUDE.md §9C
            # failure-injection: "two resume requests racing on the same run --
            # exactly one must succeed, the other must get a CAS rejection").
            logger.info("run_id=%s: stale state on admission, skipping (another worker won)", run_id)

    def _on_done(self, run_id: str, future) -> None:
        with self._lock:
            self._active_runs.discard(run_id)
            self._lease_lost.discard(run_id)
        try:
            self._persistence.release_lease(run_id, self._worker_id)
        except Exception:  # pragma: no cover - defensive
            logger.exception("release_lease failed for run_id=%s", run_id)
        self._sema.release()
        # P3 gate review: a run finishing frees a concurrency slot (or, if it
        # was the last one, makes this worker idle) -- either way the
        # admission loop must look again promptly rather than sleeping out
        # whatever is left of its current wait, so a queued run waiting on
        # exactly this slot is picked up immediately, and (with the idle
        # sweep disabled) this worker still parks on `_wake_event` rather
        # than being left with no path back to busy at all.
        self._wake_event.set()
        exc = future.exception()
        if exc is not None and not isinstance(exc, StaleStateError):
            logger.error("run_id=%s: executor task raised %r", run_id, exc)

    # ── heartbeat ────────────────────────────────────────────────────────────

    def _heartbeat_loop(self) -> None:
        interval = min(self._heartbeat_interval_s, max(1.0, self._lease_ttl_s / 3))
        while not self._stop_event.is_set():
            self._stop_event.wait(interval)
            if self._stop_event.is_set():
                return
            with self._lock:
                run_ids = [rid for rid in self._active_runs if rid not in self._lease_lost]
            now = self._clock()
            for run_id in run_ids:
                renewed = False
                try:
                    renewed = self._persistence.renew_lease(
                        run_id, self._worker_id, ttl_s=self._lease_ttl_s, now=now
                    )
                except Exception:  # pragma: no cover - defensive
                    logger.exception("renew_lease failed for run_id=%s", run_id)
                # P3 gate review item 3b: renew_lease's boolean was previously
                # ignored -- a failed renewal (lease lost to another worker,
                # lease row deleted, or the exception above) left this worker
                # believing it still held the run. worker_alive() now reflects
                # the loss so the pipeline stops before its next node write
                # (orchestrator/pipeline.py checks worker_alive() before every
                # node), rather than racing whoever holds the lease now.
                if not renewed:
                    self._mark_lease_lost(run_id, now)

    def _mark_lease_lost(self, run_id: str, now: str) -> None:
        with self._lock:
            already_marked = run_id in self._lease_lost
            self._lease_lost.add(run_id)
        if already_marked:
            return
        logger.error(
            "run_id=%s: lease renewal failed for worker %s -- halting this run's pipeline progress",
            run_id, self._worker_id,
        )
        try:
            state = self._persistence.load_state(run_id)
            self._persistence.append_trace_event(
                {
                    "event_id": _trace_event_id(run_id, "run_lease_lost", state.state_version),
                    "run_id": run_id,
                    "engagement_id": state.engagement_id,
                    "event_type": "run_lease_lost",
                    "event_time": now,
                    "stage": state.phase,
                    "status": state.status,
                    "message": (
                        f"lease renewal failed for worker {self._worker_id!r}; "
                        "pipeline execution halted for this run pending resume"
                    ),
                    "duration_s": None,
                    "node_name": None,
                    "execution_key": None,
                    "actor": self._worker_id,
                    "state_version": state.state_version,
                }
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("failed to record run_lease_lost trace event for run_id=%s", run_id)
