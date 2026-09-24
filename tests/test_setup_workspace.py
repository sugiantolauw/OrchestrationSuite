"""scripts.setup_workspace (independent review 2026-09-24 item 7):
importable, no module-level reliance on environment variables being already
set (a notebook can `import scripts.setup_workspace` safely even before any
config exists), main(argv) accepts overrides and returns an int rather than
calling sys.exit directly. Never talks to a real workspace."""

from __future__ import annotations

import scripts.setup_workspace as setup_workspace


def test_build_ddl_statements_uses_the_given_catalog_schema_volume():
    statements = setup_workspace.build_ddl_statements("cat", "sch", "/Volumes/cat/sch/uploads")
    assert statements[0] == "CREATE CATALOG IF NOT EXISTS cat"
    assert statements[1] == "CREATE SCHEMA IF NOT EXISTS cat.sch"
    assert statements[2] == "CREATE VOLUME IF NOT EXISTS cat.sch.uploads"
    assert any("cat.sch.runs" in s for s in statements)
    assert any("cat.sch.golden_examples" in s for s in statements)


def test_build_parser_defaults():
    args = setup_workspace.build_parser().parse_args([])
    assert args.catalog is None
    assert args.schema is None
    assert args.volume is None
    assert args.warehouse_id is None


def test_build_parser_overrides():
    args = setup_workspace.build_parser().parse_args([
        "--catalog", "cat", "--schema", "sch", "--volume", "/Volumes/cat/sch/v", "--warehouse-id", "wh-1",
    ])
    assert args.catalog == "cat"
    assert args.schema == "sch"
    assert args.volume == "/Volumes/cat/sch/v"
    assert args.warehouse_id == "wh-1"


def test_main_returns_1_when_configuration_is_missing(monkeypatch, capsys):
    monkeypatch.delenv("DBX_CATALOG", raising=False)
    monkeypatch.delenv("DBX_SCHEMA", raising=False)
    monkeypatch.delenv("DBX_VOLUME", raising=False)
    exit_code = setup_workspace.main([])
    assert exit_code == 1
    assert "missing required configuration" in capsys.readouterr().out


def test_main_returns_1_when_only_some_configuration_is_present(monkeypatch, capsys):
    monkeypatch.setenv("DBX_CATALOG", "cat")
    monkeypatch.delenv("DBX_SCHEMA", raising=False)
    monkeypatch.delenv("DBX_VOLUME", raising=False)
    exit_code = setup_workspace.main([])
    assert exit_code == 1
    out = capsys.readouterr().out
    assert "DBX_SCHEMA" in out
    assert "DBX_VOLUME" in out
    assert "DBX_CATALOG" not in out.split("missing required configuration:")[1]


def test_main_cli_overrides_satisfy_the_missing_check(monkeypatch):
    """--catalog/--schema/--volume satisfy the requirement check even with
    no env vars set at all -- this is as far as this test can go without a
    real WorkspaceClient; it asserts the config-completeness gate passes
    (does not return 1 for "missing configuration"), not a full bootstrap."""
    monkeypatch.delenv("DBX_CATALOG", raising=False)
    monkeypatch.delenv("DBX_SCHEMA", raising=False)
    monkeypatch.delenv("DBX_VOLUME", raising=False)

    class _RaisesOnConstruction:
        def __init__(self, *a, **kw):
            raise RuntimeError("no real workspace in this test")

    monkeypatch.setattr("databricks.sdk.WorkspaceClient", _RaisesOnConstruction)

    import pytest

    with pytest.raises(RuntimeError, match="no real workspace"):
        setup_workspace.main(["--catalog", "cat", "--schema", "sch", "--volume", "/Volumes/cat/sch/v"])
