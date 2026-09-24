"""orchestrator.readiness (independent review 2026-09-24 item 5): Volume
reachable (list/stat), warehouse reachable, configured source bindings
resolvable, model endpoints reachable (a cheap metadata call). Cached for a
TTL; every check reports a sanitized reason, never raw internal exception
text."""

from __future__ import annotations

import pytest

from orchestrator.readiness import (
    CachedReadiness,
    check_model_endpoints,
    check_source_bindings,
    check_volume,
    check_warehouse,
    run_readiness_checks,
)


class _OkPersistence:
    def find_runs(self, statuses):
        return []


class _FailingPersistence:
    def find_runs(self, statuses):
        raise RuntimeError("connection string leaked here: postgres://user:pass@host/db")


class _OkExportStorage:
    def __init__(self):
        self.files = {}

    def stat_root(self):
        return {"entry_count": 0}

    def read(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


class _FailingExportStorage:
    def stat_root(self):
        raise RuntimeError("PERMISSION_DENIED: secret detail should never reach a caller")

    def read(self, path):
        raise RuntimeError("PERMISSION_DENIED")


class _OkModelClient:
    def describe_endpoint(self, endpoint):
        return {"foundation_model": "gpt-oss-120b", "ready": True}


class _NotReadyModelClient:
    def describe_endpoint(self, endpoint):
        return {"foundation_model": "gpt-oss-120b", "ready": False}


class _FailingModelClient:
    def describe_endpoint(self, endpoint):
        raise RuntimeError("internal endpoint id 12345 should never reach a caller")


class _Settings:
    def __init__(self, model_sonnet=None, model_gpt_oss="databricks-gpt-oss-120b", enable_row_level_llm=False):
        self.model_sonnet = model_sonnet
        self.model_gpt_oss = model_gpt_oss
        self.enable_row_level_llm = enable_row_level_llm


# ── individual checks ────────────────────────────────────────────────────


def test_check_warehouse_ok():
    result = check_warehouse(_OkPersistence())
    assert result.ok


def test_check_warehouse_failure_never_leaks_internal_text(caplog):
    result = check_warehouse(_FailingPersistence())
    assert not result.ok
    assert "warehouse unreachable" == result.detail
    assert "postgres://" not in result.detail


def test_check_volume_none_export_storage_is_ok_local_backend():
    result = check_volume(None)
    assert result.ok


def test_check_volume_ok():
    assert check_volume(_OkExportStorage()).ok


def test_check_volume_failure_sanitized():
    result = check_volume(_FailingExportStorage())
    assert not result.ok
    assert result.detail == "volume unreachable"
    assert "PERMISSION_DENIED" not in result.detail


def test_check_source_bindings_ok_when_file_readable():
    storage = _OkExportStorage()
    storage.files["/Volumes/cat/sch/vol/tne/per_diem.xlsx"] = b"data"
    cfg = {"SKILL-001": {"per_diem_rates": {"kind": "volume_file", "path": "/Volumes/cat/sch/vol/tne/per_diem.xlsx"}}}
    results = check_source_bindings(cfg, storage)
    assert len(results) == 1
    assert results[0].ok


def test_check_source_bindings_missing_file_fails_sanitized():
    storage = _OkExportStorage()
    cfg = {"SKILL-001": {"per_diem_rates": {"kind": "volume_file", "path": "/Volumes/cat/sch/vol/tne/per_diem.xlsx"}}}
    results = check_source_bindings(cfg, storage)
    assert len(results) == 1
    assert not results[0].ok
    assert results[0].detail == "file unreadable"


def test_check_source_bindings_empty_file_fails():
    storage = _OkExportStorage()
    storage.files["/Volumes/cat/sch/vol/tne/per_diem.xlsx"] = b""
    cfg = {"SKILL-001": {"per_diem_rates": {"kind": "volume_file", "path": "/Volumes/cat/sch/vol/tne/per_diem.xlsx"}}}
    results = check_source_bindings(cfg, storage)
    assert not results[0].ok
    assert results[0].detail == "file is empty"


def test_check_source_bindings_ignores_uc_table_entries():
    storage = _OkExportStorage()
    cfg = {"SKILL-001": {"approval_aging": {"kind": "uc_table", "fqn": "cat.sch.approval_aging"}}}
    assert check_source_bindings(cfg, storage) == []


def test_check_source_bindings_empty_config():
    assert check_source_bindings({}, _OkExportStorage()) == []
    assert check_source_bindings(None, _OkExportStorage()) == []


def test_check_model_endpoints_ok():
    results = check_model_endpoints(_Settings(model_sonnet="databricks-claude-sonnet-5"), _OkModelClient())
    assert all(r.ok for r in results)
    names = {r.name for r in results}
    assert names == {"model_endpoint:model_sonnet", "model_endpoint:model_gpt_oss"}


def test_check_model_endpoints_unconfigured_role_is_ok():
    results = check_model_endpoints(_Settings(model_sonnet=None), _OkModelClient())
    sonnet = next(r for r in results if r.name == "model_endpoint:model_sonnet")
    assert sonnet.ok
    assert sonnet.detail == "not configured"


def test_check_model_endpoints_not_ready_fails():
    results = check_model_endpoints(_Settings(), _NotReadyModelClient())
    gpt_oss = next(r for r in results if r.name == "model_endpoint:model_gpt_oss")
    assert not gpt_oss.ok


def test_check_model_endpoints_failure_sanitized():
    results = check_model_endpoints(_Settings(), _FailingModelClient())
    gpt_oss = next(r for r in results if r.name == "model_endpoint:model_gpt_oss")
    assert not gpt_oss.ok
    assert gpt_oss.detail == "endpoint unreachable"
    assert "12345" not in gpt_oss.detail


def test_check_model_endpoints_no_client_is_ok_local_test():
    results = check_model_endpoints(_Settings(), None)
    assert all(r.ok for r in results)


def test_check_model_endpoints_not_required_for_a_plain_fieldwork_run():
    # Independent review 2026-09-24 item 2: today the only fieldwork
    # consumer of a model endpoint is classify's optional T4.3 row-level
    # LLM path, gated by enable_row_level_llm (default False). With it off,
    # the check is tagged "feature:row_level_llm", not "always" -- so it
    # never blocks /ready or a run start on its own.
    results = check_model_endpoints(
        _Settings(model_sonnet="databricks-claude-sonnet-5", enable_row_level_llm=False), _FailingModelClient()
    )
    assert all(not r.ok for r in results)
    assert all("always" not in r.required_for for r in results)
    assert all("feature:row_level_llm" in r.required_for for r in results)


def test_check_model_endpoints_required_when_row_level_llm_enabled():
    results = check_model_endpoints(
        _Settings(model_sonnet="databricks-claude-sonnet-5", enable_row_level_llm=True), _FailingModelClient()
    )
    assert all("always" in r.required_for for r in results)


# ── run_readiness_checks / report ────────────────────────────────────────


def test_run_readiness_checks_all_ok():
    report = run_readiness_checks(
        persistence=_OkPersistence(), export_storage=_OkExportStorage(), settings=_Settings(),
        source_bindings_config={}, model_client=_OkModelClient(), clock=lambda: "2026-01-01T00:00:00Z",
    )
    assert report.ready
    assert report.failing() == []
    d = report.as_dict()
    assert d["ready"] is True
    assert d["checked_at"] == "2026-01-01T00:00:00Z"


def test_run_readiness_checks_one_failure_marks_not_ready():
    report = run_readiness_checks(
        persistence=_FailingPersistence(), export_storage=_OkExportStorage(), settings=_Settings(),
        source_bindings_config={}, model_client=_OkModelClient(), clock=lambda: "t",
    )
    assert not report.ready
    assert [c.name for c in report.failing()] == ["warehouse"]


def test_run_readiness_checks_model_endpoint_failure_alone_does_not_block():
    # enable_row_level_llm defaults to False, so a model-endpoint failure is
    # reported (failing()) but does not gate .ready or blocking_failures().
    report = run_readiness_checks(
        persistence=_OkPersistence(), export_storage=_OkExportStorage(),
        settings=_Settings(model_sonnet="databricks-claude-sonnet-5"),
        source_bindings_config={}, model_client=_FailingModelClient(), clock=lambda: "t",
    )
    assert report.ready
    assert report.blocking_failures() == []
    assert len(report.failing()) == 2  # both endpoints fail, but neither blocks
    d = report.as_dict()
    assert d["ready"] is True
    endpoint_entries = [c for c in d["checks"] if c["name"].startswith("model_endpoint:")]
    assert all(c["required"] is False for c in endpoint_entries)


def test_run_readiness_checks_model_endpoint_failure_blocks_when_row_level_llm_enabled():
    report = run_readiness_checks(
        persistence=_OkPersistence(), export_storage=_OkExportStorage(),
        settings=_Settings(model_sonnet="databricks-claude-sonnet-5", enable_row_level_llm=True),
        source_bindings_config={}, model_client=_FailingModelClient(), clock=lambda: "t",
    )
    assert not report.ready
    assert len(report.blocking_failures()) == 2


# ── caching ───────────────────────────────────────────────────────────────


def test_cached_readiness_reuses_within_ttl():
    calls = {"n": 0}

    class _CountingPersistence:
        def find_runs(self, statuses):
            calls["n"] += 1
            return []

    clock_time = [0.0]
    cache = CachedReadiness(
        persistence=_CountingPersistence(), export_storage=None, settings=_Settings(),
        source_bindings_config={}, model_client=None, clock=lambda: "t", ttl_s=60,
        now_fn=lambda: clock_time[0],
    )
    cache.get()
    cache.get()
    assert calls["n"] == 1  # second call served from cache

    clock_time[0] = 61  # past the TTL
    cache.get()
    assert calls["n"] == 2


def test_cached_readiness_force_bypasses_cache():
    calls = {"n": 0}

    class _CountingPersistence:
        def find_runs(self, statuses):
            calls["n"] += 1
            return []

    cache = CachedReadiness(
        persistence=_CountingPersistence(), export_storage=None, settings=_Settings(),
        source_bindings_config={}, model_client=None, clock=lambda: "t", ttl_s=9999,
    )
    cache.get()
    cache.get(force=True)
    assert calls["n"] == 2
