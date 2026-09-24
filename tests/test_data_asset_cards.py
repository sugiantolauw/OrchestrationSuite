"""orchestrator.service.list_data_asset_cards / list_governed_tables: the
data_asset_card component must never render the literal "None" for Owner or
Refreshed (CLAUDE.md §5 UI item 3). A field is included in the shaped card
only when the source actually supplied it -- component.data_asset_card's own
existing "—" / "File" fallbacks then render, exactly as they always have
for a genuinely missing value.

Local backend: refreshed comes from the file's own mtime, owner is never
set (no such concept for a local file), rows stays unset (existing "File"
fallback). UC backend: owner/last_refreshed/classification/rows are real UC
metadata and real per-table queries, fetched only for the (optionally
limited) set of cards being shaped -- never for every governed table."""

from __future__ import annotations

import datetime
from pathlib import Path

import pandas as pd
import pytest

from orchestrator import service

REPO_ROOT = Path(__file__).parent.parent


def _local_ctx(tmp_path, skills_dir):
    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(tmp_path),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
        "SKILLS_DIR": str(skills_dir),
    }
    return service.build_app_context(env)


def _write_mini_skill(skills_dir: Path, data_root: Path) -> None:
    skill_dir = skills_dir / "mini"
    skill_dir.mkdir(parents=True)
    (skill_dir / "contract.yaml").write_text(
        "sources:\n  claims:\n    file: claims.csv\n    columns:\n      Amount: {type: float}\n"
    )
    pd.DataFrame([{"Amount": 1.0}]).to_csv(data_root / "claims.csv", index=False)


# ── local backend ────────────────────────────────────────────────────────────


def test_local_governed_table_has_last_refreshed_from_file_mtime_not_none(tmp_path):
    skills_dir = tmp_path / "skills"
    _write_mini_skill(skills_dir, tmp_path)
    ctx = _local_ctx(tmp_path, skills_dir)

    tables = service.list_governed_tables(ctx)
    claims = next(t for t in tables if t["table"] == "claims")

    assert "last_refreshed" in claims
    # A real ISO date, matching the file's own mtime (not a placeholder).
    expected = datetime.date.fromtimestamp((tmp_path / "claims.csv").stat().st_mtime).isoformat()
    assert claims["last_refreshed"] == expected
    assert "owner" not in claims  # CLAUDE.md §5 UI item 3: "owner omitted" for local files


def test_local_data_asset_card_never_carries_a_literal_none(tmp_path):
    skills_dir = tmp_path / "skills"
    _write_mini_skill(skills_dir, tmp_path)
    ctx = _local_ctx(tmp_path, skills_dir)

    cards = service.list_data_asset_cards(ctx, query="", limit=10)
    claims_card = next(c for c in cards if c["name"] == "claims.csv")

    assert None not in claims_card.values()
    assert "owner" not in claims_card
    assert "rows" not in claims_card
    assert claims_card["access"] == "Available"


def test_local_missing_file_has_no_last_refreshed(tmp_path):
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "mini"
    skill_dir.mkdir(parents=True)
    (skill_dir / "contract.yaml").write_text(
        "sources:\n  claims:\n    file: missing.csv\n    columns: {}\n"
    )
    ctx = _local_ctx(tmp_path, skills_dir)

    tables = service.list_governed_tables(ctx)
    claims = next(t for t in tables if t["table"] == "claims")
    assert "last_refreshed" not in claims
    assert claims["comment"] == "File not found"


# ── UC backend (fake data source -- no live workspace needed) ───────────────


class _FakeUCDataSource:
    def __init__(self, tables, row_counts=None, classifications=None):
        self._tables = tables
        self._row_counts = row_counts or {}
        self._classifications = classifications or {}
        self.row_count_calls: list[str] = []
        self.classification_calls: list[str] = []

    def list_tables(self, catalog=None, schema=None):
        return self._tables

    def get_row_count(self, fqn):
        self.row_count_calls.append(fqn)
        return self._row_counts.get(fqn, 0)

    def get_classification(self, fqn):
        self.classification_calls.append(fqn)
        return self._classifications.get(fqn)


def _uc_ctx(data_source):
    return service.AppContext(
        settings=None,
        persistence=None,
        skills_dir=REPO_ROOT / "skills",
        data_source_factory=lambda bindings: data_source,
        export_storage=None,
        clock=lambda: "2026-09-23T00:00:00Z",
        backend="uc",
    )


def test_uc_card_carries_real_owner_and_refreshed_when_present():
    tables = [
        {"fqn": "cat.sch.expense_report", "catalog": "cat", "schema": "sch", "table": "expense_report",
         "comment": "T&E claims", "columns": [], "owner": "alex@example.com", "last_refreshed": "2026-09-01"},
    ]
    ds = _FakeUCDataSource(tables, row_counts={"cat.sch.expense_report": 92798})
    ctx = _uc_ctx(ds)

    cards = service.list_data_asset_cards(ctx, query="", limit=10)
    assert len(cards) == 1
    card = cards[0]
    assert card["owner"] == "alex@example.com"
    assert card["last_refreshed"] == "2026-09-01"
    assert card["rows"] == 92798
    assert None not in card.values()


def test_uc_card_omits_owner_and_refreshed_when_uc_has_none_never_none_literal():
    tables = [
        {"fqn": "cat.sch.mystery", "catalog": "cat", "schema": "sch", "table": "mystery",
         "comment": None, "columns": []},
    ]
    ds = _FakeUCDataSource(tables, row_counts={"cat.sch.mystery": 3})
    ctx = _uc_ctx(ds)

    card = service.list_data_asset_cards(ctx, query="", limit=10)[0]
    assert "owner" not in card
    assert "last_refreshed" not in card
    assert None not in card.values()


def test_uc_row_count_and_classification_only_fetched_for_cards_within_limit():
    tables = [
        {"fqn": f"cat.sch.t{i}", "catalog": "cat", "schema": "sch", "table": f"t{i}",
         "comment": "", "columns": []}
        for i in range(5)
    ]
    ds = _FakeUCDataSource(tables)
    ctx = _uc_ctx(ds)

    cards = service.list_data_asset_cards(ctx, query="", limit=2)
    assert len(cards) == 2
    assert len(ds.row_count_calls) == 2
    assert len(ds.classification_calls) == 2


def test_uc_restricted_table_never_queried_for_row_count_or_classification():
    tables = [{"fqn": "locked_catalog", "restricted": True}]
    ds = _FakeUCDataSource(tables)
    ctx = _uc_ctx(ds)

    cards = service.list_data_asset_cards(ctx, query="", limit=10)
    assert cards[0]["access"] == "Restricted"
    assert ds.row_count_calls == []
    assert ds.classification_calls == []


def test_uc_classification_tag_surfaces_when_present():
    tables = [
        {"fqn": "cat.sch.expense_report", "catalog": "cat", "schema": "sch", "table": "expense_report",
         "comment": "", "columns": []},
    ]
    ds = _FakeUCDataSource(tables, classifications={"cat.sch.expense_report": "PII"})
    ctx = _uc_ctx(ds)

    card = service.list_data_asset_cards(ctx, query="", limit=10)[0]
    assert card["classification"] == "PII"


def test_uc_no_limit_never_calls_row_count_or_classification():
    """Callers that omit `limit` are expected to be doing their own
    filtering/slicing downstream (or none at all) -- list_data_asset_cards
    must not eagerly enrich a whole catalog listing (CLAUDE.md §5 UI item 3:
    "cheap ... for the cards actually rendered")."""
    tables = [
        {"fqn": f"cat.sch.t{i}", "catalog": "cat", "schema": "sch", "table": f"t{i}",
         "comment": "", "columns": []}
        for i in range(3)
    ]
    ds = _FakeUCDataSource(tables)
    ctx = _uc_ctx(ds)

    cards = service.list_data_asset_cards(ctx, query="")
    assert len(cards) == 3
    # limit=None is documented as "no row count / classification lookups at
    # all" -- see list_data_asset_cards's own docstring.
