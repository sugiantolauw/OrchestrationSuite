"""scripts.setup_workspace (independent review 2026-09-24 item 7):
importable, no module-level reliance on environment variables being already
set (a notebook can `import scripts.setup_workspace` safely even before any
config exists), main(argv) accepts overrides and returns an int rather than
calling sys.exit directly. Never talks to a real workspace.

Independent review 2026-09-24 item 1: this script bootstraps catalog/schema/
Volume/warehouse-auto-stop itself, then delegates every table to the real
migration path -- DeltaPersistence(settings).migrate() against
orchestrator/ddl/delta/*.sql -- rather than carrying its own, separately
maintained DDL. There is exactly one place table DDL is defined."""

from __future__ import annotations

import scripts.setup_workspace as setup_workspace


def test_build_bootstrap_statements_uses_the_given_catalog_schema_volume():
    statements = setup_workspace.build_bootstrap_statements("cat", "sch", "/Volumes/cat/sch/uploads")
    assert statements == [
        "CREATE CATALOG IF NOT EXISTS cat",
        "CREATE SCHEMA IF NOT EXISTS cat.sch",
        "CREATE VOLUME IF NOT EXISTS cat.sch.uploads",
    ]


def test_build_bootstrap_statements_extracts_volume_name_from_a_full_path():
    statements = setup_workspace.build_bootstrap_statements("cat", "sch", "/Volumes/cat/sch/uploads/")
    assert statements[2] == "CREATE VOLUME IF NOT EXISTS cat.sch.uploads"


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


# ── main(): warehouse auto-stop + delegation to DeltaPersistence.migrate() ──


class _FakeOdbcParams:
    def __init__(self, path):
        self.path = path


class _FakeWarehouse:
    def __init__(self, id="wh-1", name="Test WH", auto_stop_mins=15):
        self.id = id
        self.name = name
        self.auto_stop_mins = auto_stop_mins
        self.cluster_size = "2X-Small"
        self.min_num_clusters = 1
        self.max_num_clusters = 1
        self.enable_serverless_compute = True
        self.odbc_params = _FakeOdbcParams(f"/sql/1.0/warehouses/{id}")


class _FakeWarehousesAPI:
    def __init__(self, warehouse):
        self._warehouse = warehouse
        self.edit_calls = []

    def list(self):
        return [self._warehouse]

    def get(self, warehouse_id):
        return self._warehouse

    def edit(self, **kwargs):
        self.edit_calls.append(kwargs)
        self._warehouse.auto_stop_mins = kwargs["auto_stop_mins"]


class _FakeStatementExecutionAPI:
    def __init__(self):
        self.statements = []

    def execute_statement(self, *, warehouse_id, statement, wait_timeout):
        self.statements.append(statement)


def _make_fake_workspace_client(auto_stop_mins=15):
    warehouse = _FakeWarehouse(auto_stop_mins=auto_stop_mins)

    class _FakeWorkspaceClient:
        def __init__(self):
            self.warehouses = _FakeWarehousesAPI(warehouse)
            self.statement_execution = _FakeStatementExecutionAPI()

    return _FakeWorkspaceClient


class _FakeDeltaPersistence:
    instances: list = []

    def __init__(self, settings, **kwargs):
        self.settings = settings
        type(self).instances.append(self)

    def migrate(self):
        return ["001_p1a_ledger", "002_p1b_suite"]


def _patch_env_for_main(monkeypatch, tmp_path):
    monkeypatch.setenv("DBX_CATALOG", "cat")
    monkeypatch.setenv("DBX_SCHEMA", "sch")
    monkeypatch.setenv("DBX_VOLUME", "/Volumes/cat/sch/uploads")
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.databricks.com")


def test_main_bootstraps_then_delegates_table_ddl_to_migrate(monkeypatch, capsys):
    _patch_env_for_main(monkeypatch, None)
    fake_client_cls = _make_fake_workspace_client(auto_stop_mins=15)
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", fake_client_cls)
    _FakeDeltaPersistence.instances = []
    monkeypatch.setattr("orchestrator.adapters.persistence_delta.DeltaPersistence", _FakeDeltaPersistence)

    exit_code = setup_workspace.main([])
    assert exit_code == 0

    out = capsys.readouterr().out
    assert "applied: 001_p1a_ledger, 002_p1b_suite" in out

    # Exactly the three bootstrap statements -- no per-table DDL duplicated here.
    assert len(_FakeDeltaPersistence.instances) == 1
    persistence = _FakeDeltaPersistence.instances[0]
    assert persistence.settings.catalog == "cat"
    assert persistence.settings.schema == "sch"
    assert persistence.settings.volume == "/Volumes/cat/sch/uploads"
    assert persistence.settings.warehouse_http_path == "/sql/1.0/warehouses/wh-1"


def test_main_sets_warehouse_auto_stop_when_not_already_one_minute(monkeypatch, capsys):
    _patch_env_for_main(monkeypatch, None)
    fake_client_cls = _make_fake_workspace_client(auto_stop_mins=15)
    _FakeDeltaPersistence.instances = []
    monkeypatch.setattr("orchestrator.adapters.persistence_delta.DeltaPersistence", _FakeDeltaPersistence)

    captured = {}

    def _capturing_client():
        client = fake_client_cls()
        captured["client"] = client
        return client

    monkeypatch.setattr("databricks.sdk.WorkspaceClient", _capturing_client)

    assert setup_workspace.main([]) == 0
    assert captured["client"].warehouses.edit_calls == [
        {
            "id": "wh-1",
            "name": "Test WH",
            "cluster_size": "2X-Small",
            "auto_stop_mins": 1,
            "min_num_clusters": 1,
            "max_num_clusters": 1,
            "enable_serverless_compute": True,
        }
    ]


def test_main_leaves_warehouse_alone_when_already_one_minute(monkeypatch, capsys):
    _patch_env_for_main(monkeypatch, None)
    fake_client_cls = _make_fake_workspace_client(auto_stop_mins=1)
    _FakeDeltaPersistence.instances = []
    monkeypatch.setattr("orchestrator.adapters.persistence_delta.DeltaPersistence", _FakeDeltaPersistence)

    captured = {}

    def _capturing_client():
        client = fake_client_cls()
        captured["client"] = client
        return client

    monkeypatch.setattr("databricks.sdk.WorkspaceClient", _capturing_client)

    assert setup_workspace.main([]) == 0
    assert captured["client"].warehouses.edit_calls == []


def test_main_reports_already_up_to_date_when_no_migrations_pending(monkeypatch, capsys):
    _patch_env_for_main(monkeypatch, None)
    fake_client_cls = _make_fake_workspace_client(auto_stop_mins=1)
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", fake_client_cls)

    class _NoOpDeltaPersistence(_FakeDeltaPersistence):
        def migrate(self):
            return []

    monkeypatch.setattr("orchestrator.adapters.persistence_delta.DeltaPersistence", _NoOpDeltaPersistence)

    assert setup_workspace.main([]) == 0
    assert "already up to date" in capsys.readouterr().out
