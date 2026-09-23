from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

import pytest

from orchestrator.adapters.persistence_local import LocalPersistence
from orchestrator.config import load_settings

REPO_ROOT = Path(__file__).resolve().parent.parent

# The databricks-sql-connector (and its thrift transport) log at DEBUG by default and
# flood pytest output on every live run. Quiet them here rather than per-invocation.
for _name in ("databricks.sql", "databricks.sql.thrift_backend", "databricks.sdk", "thrift"):
    logging.getLogger(_name).setLevel(logging.WARNING)


def canonical_ts(n: int = 0) -> str:
    """A deterministic canonical (YYYY-MM-DDTHH:MM:SS.ffffffZ) timestamp for tests,
    replacing the old 't0'/'t1' placeholders that validate() now rejects."""
    minutes, seconds = divmod(n, 60)
    return f"2026-01-01T00:{minutes:02d}:{seconds:02d}.000000Z"


@pytest.fixture
def clock():
    state = {"n": 0}

    def _clock() -> str:
        state["n"] += 1
        return canonical_ts(state["n"])

    return _clock


def _local_memory_persistence():
    p = LocalPersistence(":memory:")
    p.migrate()
    return p


def _local_file_persistence(tmp_path):
    db_path = str(tmp_path / "ledger.db")
    p = LocalPersistence(db_path)
    p.migrate()
    return p


def _delta_persistence():
    from orchestrator.adapters.persistence_delta import DeltaPersistence

    settings = load_settings()
    p = DeltaPersistence(settings)
    p.migrate()
    return p


@pytest.fixture(scope="session")
def delta_schema():
    """One throwaway schema for the whole pytest session: created and migrated once
    here, dropped at session end. Individual tests get isolation from each other via
    unique ids (the `uid` fixture below), not a fresh schema per test — Delta migrate()
    against a brand-new schema takes over a minute (14 tables, ~25 CHECK constraints),
    so a fresh schema per test would make the live suite impractically slow.

    DBX_SCHEMA is overridden in-process for the session so every DeltaPersistence built
    via load_settings() (including the `persistence` fixture below) picks up the
    throwaway schema transparently, regardless of what DBX_SCHEMA was set to on entry.

    The throwaway schema's name is derived from the configured DBX_SCHEMA (never a
    hardcoded literal, CLAUDE.md §3 non-negotiable 16 / §7 portability) with a `_test_`
    suffix, so it can never collide with or equal the real one.
    """
    from databricks import sql as dbsql
    from databricks.sdk.core import Config

    base_settings = load_settings()
    base_settings.require("catalog", "schema", "warehouse_http_path", "host")
    schema = f"{base_settings.schema}_test_{uuid.uuid4().hex[:8]}"
    assert schema != base_settings.schema and schema.startswith(f"{base_settings.schema}_test_")

    original_env = os.environ.get("DBX_SCHEMA")
    os.environ["DBX_SCHEMA"] = schema

    def _connect():
        cfg = Config(host=base_settings.host)
        hostname = base_settings.host.replace("https://", "").replace("http://", "").rstrip("/")
        return dbsql.connect(
            server_hostname=hostname,
            http_path=base_settings.warehouse_http_path,
            credentials_provider=lambda: cfg.authenticate,
        )

    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {base_settings.catalog}.{schema}")
        cur.close()
    finally:
        conn.close()

    from orchestrator.adapters.persistence_delta import DeltaPersistence

    DeltaPersistence(load_settings()).migrate()

    try:
        yield schema
    finally:
        conn = _connect()
        try:
            cur = conn.cursor()
            cur.execute(f"DROP SCHEMA IF EXISTS {base_settings.catalog}.{schema} CASCADE")
            cur.close()
        finally:
            conn.close()
        if original_env is None:
            os.environ.pop("DBX_SCHEMA", None)
        else:
            os.environ["DBX_SCHEMA"] = original_env


@pytest.fixture(params=["local_memory", "local_file", "delta"])
def persistence(request, tmp_path):
    kind = request.param
    if kind == "local_memory":
        return _local_memory_persistence()
    if kind == "local_file":
        return _local_file_persistence(tmp_path)
    if kind == "delta":
        if os.environ.get("RUN_DELTA_TESTS") != "1":
            pytest.skip("RUN_DELTA_TESTS not set — live workspace is unavailable (CLAUDE.md §11)")
        request.getfixturevalue("delta_schema")
        return _delta_persistence()
    raise ValueError(kind)


@pytest.fixture
def delta_settings(request):
    """Settings pointed at the session's throwaway Delta schema, for tests that need
    more than one DeltaPersistence instance/connection directly (concurrency tests) or
    that are delta-only and skip on every other backend."""
    if os.environ.get("RUN_DELTA_TESTS") != "1":
        pytest.skip("RUN_DELTA_TESTS not set — live workspace is unavailable (CLAUDE.md §11)")
    request.getfixturevalue("delta_schema")
    return load_settings()


@pytest.fixture
def uid():
    """A short unique suffix for building run/fingerprint/event ids. Local backends get
    a fresh database per test so hardcoded ids never collided there, but the `delta`
    backend shares one schema for the whole session (see `delta_schema` above) — every
    id a test writes must be unique across the session, not just within the test."""
    return uuid.uuid4().hex[:8]


@pytest.fixture(params=["local_memory", "local_file"])
def local_persistence(request, tmp_path):
    if request.param == "local_memory":
        return _local_memory_persistence()
    return _local_file_persistence(tmp_path)


@pytest.fixture
def local_persistence_db_path(tmp_path):
    return str(tmp_path / "ledger.db")
