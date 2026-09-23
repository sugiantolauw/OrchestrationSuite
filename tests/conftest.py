from __future__ import annotations

import os
from pathlib import Path

import pytest

from orchestrator.adapters.persistence_local import LocalPersistence
from orchestrator.config import load_settings

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def clock():
    state = {"n": 0}

    def _clock() -> str:
        state["n"] += 1
        minutes, seconds = divmod(state["n"], 60)
        return f"2026-01-01T00:{minutes:02d}:{seconds:02d}.000000+00:00"

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
        return _delta_persistence()
    raise ValueError(kind)


@pytest.fixture(params=["local_memory", "local_file"])
def local_persistence(request, tmp_path):
    if request.param == "local_memory":
        return _local_memory_persistence()
    return _local_file_persistence(tmp_path)


@pytest.fixture
def local_persistence_db_path(tmp_path):
    return str(tmp_path / "ledger.db")
