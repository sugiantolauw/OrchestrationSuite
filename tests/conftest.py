from __future__ import annotations

import dataclasses
import itertools
import os
import uuid
from pathlib import Path

import pytest

from orchestrator.adapters.persistence_local import LocalPersistence
from orchestrator.config import load_settings

REPO_ROOT = Path(__file__).resolve().parent.parent


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


_uid_counter = itertools.count(1)


@pytest.fixture
def uid():
    """A monotonically increasing id generator, unique for the whole test session --
    not just the current test. Needed because the live 'delta' backend (unlike
    local_memory/local_file) is one shared throwaway schema for the entire session, so
    two tests using a fixed literal id (e.g. 'RUN-1') would collide."""

    def _uid(prefix: str = "ID") -> str:
        return f"{prefix}-{next(_uid_counter):06d}"

    return _uid


def _local_memory_persistence():
    p = LocalPersistence(":memory:")
    p.migrate()
    return p


def _local_file_persistence(tmp_path):
    db_path = str(tmp_path / "ledger.db")
    p = LocalPersistence(db_path)
    p.migrate()
    return p


def _databricks_sql_connect(settings):
    from databricks import sql
    from databricks.sdk.core import Config

    cfg = Config(host=settings.host)
    hostname = settings.host.replace("https://", "").replace("http://", "").rstrip("/")
    return sql.connect(
        server_hostname=hostname,
        http_path=settings.warehouse_http_path,
        credentials_provider=lambda: cfg.authenticate,
    )


@pytest.fixture(scope="session")
def _delta_test_settings():
    """Session-scoped throwaway schema for live Delta tests. Every session that opts in
    via RUN_DELTA_TESTS=1 gets its own '{DBX_SCHEMA}_test_<random>' schema in the
    configured catalog, created here and dropped at session end -- so a live test run
    never migrates, writes to, or drops the real DBX_SCHEMA (CLAUDE.md §11: the real
    schema is precious and out of bounds for tests)."""
    if os.environ.get("RUN_DELTA_TESTS") != "1":
        pytest.skip("RUN_DELTA_TESTS not set — live workspace is unavailable (CLAUDE.md §11)")
    base = load_settings()
    base.require("catalog", "schema", "warehouse_http_path", "host")
    schema_name = f"{base.schema}_test_{uuid.uuid4().hex[:8]}"
    throwaway = dataclasses.replace(base, schema=schema_name)

    conn = _databricks_sql_connect(base)
    try:
        cur = conn.cursor()
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {base.catalog}.{schema_name}")
        cur.close()
        yield throwaway
    finally:
        cur = conn.cursor()
        cur.execute(f"DROP SCHEMA IF EXISTS {base.catalog}.{schema_name} CASCADE")
        cur.close()
        conn.close()


def _delta_persistence(settings):
    from orchestrator.adapters.persistence_delta import DeltaPersistence

    p = DeltaPersistence(settings)
    p.migrate()
    return p


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
        settings = request.getfixturevalue("_delta_test_settings")
        return _delta_persistence(settings)
    raise ValueError(kind)


@pytest.fixture(params=["local_memory", "local_file"])
def local_persistence(request, tmp_path):
    if request.param == "local_memory":
        return _local_memory_persistence()
    return _local_file_persistence(tmp_path)


@pytest.fixture
def local_persistence_db_path(tmp_path):
    return str(tmp_path / "ledger.db")
