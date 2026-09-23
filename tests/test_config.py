from __future__ import annotations

import dataclasses

import pytest

from orchestrator.config import Settings, load_settings, runtime_config_hash
from orchestrator.errors import ConfigError


def test_require_lists_all_missing_at_once():
    settings = Settings()
    with pytest.raises(ConfigError) as exc:
        settings.require("catalog", "schema", "warehouse_http_path", "host")
    msg = str(exc.value)
    for name in ("catalog", "schema", "warehouse_http_path", "host"):
        assert name in msg


def test_require_passes_when_all_present():
    settings = Settings(catalog="cat", schema="sch", warehouse_http_path="/x", host="https://x")
    settings.require("catalog", "schema", "warehouse_http_path", "host")


def test_identifier_validation_rejects_injection():
    with pytest.raises(ConfigError):
        Settings(catalog="x; DROP TABLE foo;--")
    with pytest.raises(ConfigError):
        Settings(schema="a b")
    with pytest.raises(ConfigError):
        Settings(catalog="1abc")


def test_identifier_validation_allows_valid_identifiers():
    Settings(catalog="governed_catalog2", schema="_hidden")


def test_runtime_config_hash_stable_for_same_settings():
    s1 = Settings(catalog="cat", schema="sch", model_sonnet="m1")
    s2 = Settings(catalog="cat", schema="sch", model_sonnet="m1")
    assert runtime_config_hash(s1) == runtime_config_hash(s2)


def test_runtime_config_hash_changes_with_a_field():
    s1 = Settings(catalog="cat", schema="sch")
    s2 = dataclasses.replace(s1, max_concurrent_runs=5)
    assert runtime_config_hash(s1) != runtime_config_hash(s2)


def test_runtime_config_hash_excludes_secrets():
    # Settings has no token field at all -- a token can never enter the hash.
    assert not hasattr(Settings(), "token")
    assert not hasattr(Settings(), "databricks_token")


def test_load_settings_reads_env_and_defaults():
    env = {
        "DBX_CATALOG": "cat1",
        "DBX_SCHEMA": "sch1",
        "DBX_VOLUME": "/Volumes/cat1/sch1/uploads",
        "DBX_WAREHOUSE_HTTP_PATH": "/sql/1.0/warehouses/abc",
        "DATABRICKS_HOST": "https://x.cloud.databricks.com",
        "DBX_APP_NAME": "ai-audit-analyst",
        "MODEL_SONNET": "databricks-claude-sonnet",
        "MODEL_GPT_OSS": "databricks-gpt-oss",
        "MAX_CONCURRENT_RUNS": "4",
        "DEMO_MODE": "true",
    }
    settings = load_settings(env)
    assert settings.catalog == "cat1"
    assert settings.schema == "sch1"
    assert settings.executor == "thread"
    assert settings.max_concurrent_runs == 4
    assert settings.demo_mode is True


def test_load_settings_defaults_when_env_empty():
    settings = load_settings({})
    assert settings.executor == "thread"
    assert settings.max_concurrent_runs == 2
    assert settings.demo_mode is False
    assert settings.catalog is None
