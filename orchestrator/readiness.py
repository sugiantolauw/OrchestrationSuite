"""Readiness checks (independent review 2026-09-24 item 5): on App start and
via `/ready`, check that the Volume is reachable (list/stat), the warehouse
is reachable (a cheap query), every configured source binding resolves
(files exist, hashes readable), and the configured model endpoints are
reachable (a cheap metadata call -- `describe_endpoint`, never a
completion). Results are cached for `Settings.readiness_cache_ttl_s`
seconds: CLAUDE.md §11's cost incident was exactly this shape -- a
health-check style endpoint re-probing the warehouse/Volume/model endpoints
on every poll. No internal error text is ever returned to a caller: every
check reports a short, sanitized reason category, and the real exception is
logged server-side only (the same discipline orchestrator.service.ready()
already uses)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from orchestrator.source_bindings import volume_file_paths

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str | None = None


@dataclass(frozen=True)
class ReadinessReport:
    checks: tuple[CheckResult, ...]
    checked_at: str

    @property
    def ready(self) -> bool:
        return all(c.ok for c in self.checks)

    def failing(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.ok]

    def as_dict(self) -> dict:
        return {
            "ready": self.ready,
            "checked_at": self.checked_at,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
        }


def check_warehouse(persistence) -> CheckResult:
    try:
        persistence.find_runs(["queued"])
        return CheckResult("warehouse", True)
    except Exception:
        _LOG.exception("readiness: warehouse check failed")
        return CheckResult("warehouse", False, "warehouse unreachable")


def check_volume(export_storage) -> CheckResult:
    if export_storage is None:
        return CheckResult("volume", True, "not configured (local backend)")
    stat_root = getattr(export_storage, "stat_root", None)
    if stat_root is None:
        return CheckResult("volume", True, "storage adapter has no stat_root() -- assumed local/test")
    try:
        stat_root()
        return CheckResult("volume", True)
    except Exception:
        _LOG.exception("readiness: volume check failed")
        return CheckResult("volume", False, "volume unreachable")


def check_source_bindings(source_bindings_config: dict, export_storage) -> list[CheckResult]:
    results: list[CheckResult] = []
    for skill_id, bindings in (source_bindings_config or {}).items():
        for path, binding in volume_file_paths(bindings).items():
            name = f"source_binding:{skill_id}"
            if export_storage is None:
                results.append(CheckResult(name, False, "no export storage configured to read it"))
                continue
            try:
                data = export_storage.read(path)
                if not data:
                    results.append(CheckResult(name, False, "file is empty"))
                else:
                    results.append(CheckResult(name, True))
            except Exception:
                _LOG.exception("readiness: source binding check failed for %s (%s)", name, path)
                results.append(CheckResult(name, False, "file unreadable"))
    return results


def check_model_endpoints(settings, model_client) -> list[CheckResult]:
    results: list[CheckResult] = []
    for role in ("model_sonnet", "model_gpt_oss"):
        endpoint = getattr(settings, role, None)
        name = f"model_endpoint:{role}"
        if not endpoint:
            results.append(CheckResult(name, True, "not configured"))
            continue
        if model_client is None:
            results.append(CheckResult(name, True, "no model client configured -- assumed local/test"))
            continue
        try:
            info = model_client.describe_endpoint(endpoint)
            ready = bool(info.get("ready"))
            results.append(CheckResult(name, ready, None if ready else "endpoint not ready"))
        except Exception:
            _LOG.exception("readiness: model endpoint check failed for %s (%s)", role, endpoint)
            results.append(CheckResult(name, False, "endpoint unreachable"))
    return results


def run_readiness_checks(
    *, persistence, export_storage, settings, source_bindings_config: dict,
    model_client, clock: Callable[[], str],
) -> ReadinessReport:
    checks = [check_warehouse(persistence), check_volume(export_storage)]
    checks += check_source_bindings(source_bindings_config, export_storage)
    checks += check_model_endpoints(settings, model_client)
    return ReadinessReport(checks=tuple(checks), checked_at=clock())


@dataclass
class CachedReadiness:
    """Wraps run_readiness_checks with a TTL cache (independent review item
    5 -- cost). `now_fn` defaults to `time.monotonic` so the cache's own
    clock is independent of `clock` (which produces the report's
    human-readable `checked_at` timestamp)."""

    persistence: Any
    export_storage: Any
    settings: Any
    source_bindings_config: dict
    model_client: Any
    clock: Callable[[], str]
    ttl_s: float = 120.0
    now_fn: Callable[[], float] = field(default=time.monotonic)
    _cached: ReadinessReport | None = field(default=None, init=False, repr=False, compare=False)
    _cached_at: float = field(default=0.0, init=False, repr=False, compare=False)

    def get(self, *, force: bool = False) -> ReadinessReport:
        now = self.now_fn()
        if not force and self._cached is not None and (now - self._cached_at) < self.ttl_s:
            return self._cached
        report = run_readiness_checks(
            persistence=self.persistence, export_storage=self.export_storage, settings=self.settings,
            source_bindings_config=self.source_bindings_config, model_client=self.model_client,
            clock=self.clock,
        )
        self._cached = report
        self._cached_at = now
        return report
