from __future__ import annotations

import dataclasses

import pytest

from orchestrator.config import DEFAULT_PPTX_TEMPLATE_PATH, Settings, load_settings, runtime_config_hash
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
    s2 = dataclasses.replace(s1, demo_mode=True)
    assert runtime_config_hash(s1) != runtime_config_hash(s2)


def test_runtime_config_hash_excludes_operational_knobs():
    # max_concurrent_runs and executor change how a run is scheduled, never what it
    # computes -- CLAUDE.md §4.1 non-blocking item, two runs differing only in these
    # should fingerprint identically.
    s1 = Settings(catalog="cat", schema="sch", max_concurrent_runs=2, executor="thread")
    s2 = dataclasses.replace(s1, max_concurrent_runs=7, executor="jobs")
    assert runtime_config_hash(s1) == runtime_config_hash(s2)


def test_runtime_config_hash_excludes_admission_backoff_knobs():
    # P3 gate review item 3a: bounded admission retries are scheduling
    # behaviour, not computation -- two runs differing only in these must
    # fingerprint identically, same as max_concurrent_runs/executor above.
    s1 = Settings(
        catalog="cat", schema="sch",
        admission_max_attempts=20, admission_backoff_base_s=2.0, admission_backoff_max_s=60.0,
    )
    s2 = dataclasses.replace(
        s1, admission_max_attempts=3, admission_backoff_base_s=0.5, admission_backoff_max_s=5.0,
    )
    assert runtime_config_hash(s1) == runtime_config_hash(s2)


def test_load_settings_reads_admission_backoff_env():
    env = {
        "ADMISSION_MAX_ATTEMPTS": "5",
        "ADMISSION_BACKOFF_BASE_S": "1.5",
        "ADMISSION_BACKOFF_MAX_S": "30",
    }
    settings = load_settings(env)
    assert settings.admission_max_attempts == 5
    assert settings.admission_backoff_base_s == 1.5
    assert settings.admission_backoff_max_s == 30.0


def test_load_settings_admission_backoff_defaults_when_env_empty():
    settings = load_settings({})
    assert settings.admission_max_attempts == 20
    assert settings.admission_backoff_base_s == 2.0
    assert settings.admission_backoff_max_s == 60.0


def test_load_settings_reads_llm_retry_env():
    env = {"LLM_MAX_TRANSPORT_ATTEMPTS": "7", "LLM_RETRY_BACKOFF_MAX_S": "90"}
    settings = load_settings(env)
    assert settings.llm_max_transport_attempts == 7
    assert settings.llm_retry_backoff_max_s == 90.0


def test_load_settings_llm_retry_defaults_when_env_empty():
    # Round-4 narration-content fix (task item 2): raised from 3/30s so the
    # worst-case total backoff (75s, see orchestrator/config.py's own
    # comment on these two fields) outlasts a Databricks Model Serving
    # per-minute rate-limit window.
    settings = load_settings({})
    assert settings.llm_max_transport_attempts == 5
    assert settings.llm_retry_backoff_max_s == 75.0


def test_runtime_config_hash_excludes_secrets():
    # Settings has no token field at all -- a token can never enter the hash.
    assert not hasattr(Settings(), "token")
    assert not hasattr(Settings(), "databricks_token")


def test_runtime_config_hash_excludes_mlflow_settings():
    # MLflow on the platform item (CLAUDE.md P2/P3 gate review): tracking
    # URI/experiment path are operational -- where spans land, never what a
    # run computes -- so two identically-configured runs differing only in
    # these must fingerprint identically, same as executor/max_concurrent_runs.
    s1 = Settings(catalog="cat", schema="sch", mlflow_tracking_uri="databricks", mlflow_experiment_path="/Shared/a")
    s2 = dataclasses.replace(s1, mlflow_tracking_uri="sqlite:///x.db", mlflow_experiment_path="/Shared/b")
    assert runtime_config_hash(s1) == runtime_config_hash(s2)


def test_load_settings_reads_mlflow_experiment_path_env():
    settings = load_settings({"MLFLOW_EXPERIMENT_PATH": "/Shared/ai-audit-analyst-audit-runs"})
    assert settings.mlflow_experiment_path == "/Shared/ai-audit-analyst-audit-runs"


def test_load_settings_mlflow_experiment_path_defaults_to_none_when_env_empty():
    assert load_settings({}).mlflow_experiment_path is None


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
    assert settings.pptx_template_path == DEFAULT_PPTX_TEMPLATE_PATH


def test_load_settings_reads_pptx_template_path_env():
    settings = load_settings({"PPTX_TEMPLATE_PATH": "/tmp/custom_template.pptx"})
    assert settings.pptx_template_path == "/tmp/custom_template.pptx"


def test_runtime_config_hash_excludes_pptx_template_path():
    # CLAUDE.md §4.7: the template changes the export's appearance only,
    # never a number or a finding -- two identically-configured runs
    # differing only in this must fingerprint identically.
    s1 = Settings(catalog="cat", schema="sch", pptx_template_path="/a/template.pptx")
    s2 = dataclasses.replace(s1, pptx_template_path="/b/other.pptx")
    assert runtime_config_hash(s1) == runtime_config_hash(s2)
