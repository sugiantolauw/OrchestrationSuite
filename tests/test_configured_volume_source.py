"""Configured Volume-file sources (independent review 2026-09-24 item 1;
CLAUDE.md §11 "Corporate workspace assessment" -- at the corporate
workspace, T&E sources are Excel files in a UC Volume, not Delta tables).

Two things are exercised:

1. `orchestrator.source_bindings.load_source_bindings` -- parsing and
   validating the SOURCE_BINDINGS config file (exact paths only, never
   filename guessing).
2. `orchestrator.adapters.datasource_volume_upload.VolumeUploadAwareDataSource`
   with `configured_volume_paths` populated -- reading a configured Volume
   file via a fake Files client, including multi-sheet xlsx, a hash
   mismatch, a missing file, and a header-whitespace contract rule.
"""

from __future__ import annotations

import hashlib
import io
import json
import threading
import time

import pandas as pd
import pytest
import yaml

from orchestrator.adapters.datasource_volume_upload import VolumeUploadAwareDataSource
from orchestrator.contract import SourceVersionMismatch
from orchestrator.errors import ConfigError, ConfiguredSourceUnavailable
from orchestrator.source_bindings import (
    bindings_for_skill,
    load_source_bindings,
    suggested_values,
    volume_file_paths,
)

CSV_BYTES = b"Employee ID,Amount\n1,100\n2,200\n"
CSV_SHA256 = hashlib.sha256(CSV_BYTES).hexdigest()
VOLUME_PATH = "/Volumes/cat/sch/vol/tne/expense_report.csv"


class _ExplodingTableSource:
    def resolve_version(self, source):
        raise AssertionError(f"table_source.resolve_version called for {source!r}")

    def read_population(self, source, **kwargs):
        raise AssertionError(f"table_source.read_population called for {source!r}")


class _FakePersistence:
    def list_uploaded_files(self, engagement_id=None):
        return []


class _FakeExportStorage:
    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.read_calls: list[str] = []

    def read(self, path):
        self.read_calls.append(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


def _ds(files, volume_file_bindings, contract_sources=None, bindings=None, table_source=None):
    bindings = bindings if bindings is not None else {"expense_report": VOLUME_PATH}
    return VolumeUploadAwareDataSource(
        table_source=table_source or _ExplodingTableSource(),
        bindings=bindings,
        contract_sources=contract_sources or {},
        persistence=_FakePersistence(),
        export_storage=_FakeExportStorage(files),
        configured_volume_paths=volume_file_bindings,
    )


def _xlsx_bytes(sheets: dict[str, dict]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf) as writer:
        for name, data in sheets.items():
            pd.DataFrame(data).to_excel(writer, sheet_name=name, index=False)
    return buf.getvalue()


# ── VolumeUploadAwareDataSource + configured_volume_paths ──────────────────


def test_resolve_version_computes_the_hash_by_reading_the_file():
    ds = _ds({VOLUME_PATH: CSV_BYTES}, {VOLUME_PATH: {"path": VOLUME_PATH}})
    assert ds.resolve_version("expense_report") == CSV_SHA256


def test_read_population_reads_via_export_storage_and_verifies_sha256():
    ds = _ds({VOLUME_PATH: CSV_BYTES}, {VOLUME_PATH: {"path": VOLUME_PATH}})
    df = ds.read_population("expense_report", version=CSV_SHA256)
    assert list(df["Amount"]) == [100, 200]
    assert ds.export_storage.read_calls == [VOLUME_PATH]


def test_read_population_rejects_a_stale_pinned_version():
    ds = _ds({VOLUME_PATH: CSV_BYTES}, {VOLUME_PATH: {"path": VOLUME_PATH}})
    with pytest.raises(SourceVersionMismatch):
        ds.read_population("expense_report", version="not-the-real-hash")


def test_read_population_rejects_content_that_changed_since_resolve_version():
    ds = _ds({VOLUME_PATH: CSV_BYTES}, {VOLUME_PATH: {"path": VOLUME_PATH}})
    pinned = ds.resolve_version("expense_report")
    ds.export_storage.files[VOLUME_PATH] = b"Employee ID,Amount\n9,999\n"
    with pytest.raises(SourceVersionMismatch):
        ds.read_population("expense_report", version=pinned)


def test_missing_file_raises_configured_source_unavailable():
    ds = _ds({}, {VOLUME_PATH: {"path": VOLUME_PATH}})
    with pytest.raises(ConfiguredSourceUnavailable) as exc_info:
        ds.resolve_version("expense_report")
    assert "expense_report" in str(exc_info.value)
    assert VOLUME_PATH in str(exc_info.value)
    assert "no bundled fallback" in str(exc_info.value)


def test_missing_file_at_read_population_also_raises():
    ds = _ds({}, {VOLUME_PATH: {"path": VOLUME_PATH}})
    with pytest.raises(ConfiguredSourceUnavailable):
        ds.read_population("expense_report", version="whatever")


def test_xlsx_with_declared_sheet():
    xlsx = _xlsx_bytes({"Sheet1": {"Employee ID": [1]}, "Sheet2": {"Employee ID": [2, 3]}})
    path = "/Volumes/cat/sch/vol/tne/rates.xlsx"
    ds = _ds(
        {path: xlsx}, {path: {"path": path, "sheet": "Sheet2"}},
        bindings={"per_diem_rates": path},
    )
    version = ds.resolve_version("per_diem_rates")
    df = ds.read_population("per_diem_rates", version=version)
    assert list(df["Employee ID"]) == [2, 3]


def test_xlsx_defaults_to_the_first_sheet_when_none_declared():
    xlsx = _xlsx_bytes({"Sheet1": {"Employee ID": [1]}, "Sheet2": {"Employee ID": [2, 3]}})
    path = "/Volumes/cat/sch/vol/tne/rates.xlsx"
    ds = _ds({path: xlsx}, {path: {"path": path}}, bindings={"per_diem_rates": path})
    version = ds.resolve_version("per_diem_rates")
    df = ds.read_population("per_diem_rates", version=version)
    assert list(df["Employee ID"]) == [1]


def test_header_whitespace_trimmed_when_contract_declares_it():
    csv_bytes = b"Employee ID , Advance Purchase Days \n1,5\n"
    path = "/Volumes/cat/sch/vol/tne/booking.csv"
    ds = _ds(
        {path: csv_bytes}, {path: {"path": path}},
        contract_sources={"booking_detail": {"format": "csv", "header_trim": True}},
        bindings={"booking_detail": path},
    )
    version = ds.resolve_version("booking_detail")
    df = ds.read_population("booking_detail", version=version)
    assert "Advance Purchase Days" in df.columns  # trimmed, not "Advance Purchase Days "


def test_header_mismatch_without_header_trim_keeps_the_raw_whitespace():
    csv_bytes = b"Employee ID , Advance Purchase Days \n1,5\n"
    path = "/Volumes/cat/sch/vol/tne/booking.csv"
    ds = _ds(
        {path: csv_bytes}, {path: {"path": path}},
        contract_sources={"booking_detail": {"format": "csv"}},
        bindings={"booking_detail": path},
    )
    version = ds.resolve_version("booking_detail")
    df = ds.read_population("booking_detail", version=version)
    assert "Advance Purchase Days" not in df.columns
    assert " Advance Purchase Days " in df.columns


def test_cell_ceiling_still_enforced_for_configured_sources():
    ds = _ds({VOLUME_PATH: CSV_BYTES}, {VOLUME_PATH: {"path": VOLUME_PATH}})
    ds.max_cells = 1
    ds.__post_init__()
    with pytest.raises(ValueError, match="DBX_MAX_CELLS"):
        ds.read_population("expense_report", version=CSV_SHA256)


def test_filters_are_rejected_for_configured_sources_too():
    ds = _ds({VOLUME_PATH: CSV_BYTES}, {VOLUME_PATH: {"path": VOLUME_PATH}})
    with pytest.raises(ValueError, match="cannot push filters down"):
        ds.read_population("expense_report", version=CSV_SHA256, filters={"x": 1})


def test_row_count_and_column_stats_go_through_the_configured_path():
    ds = _ds({VOLUME_PATH: CSV_BYTES}, {VOLUME_PATH: {"path": VOLUME_PATH}})
    assert ds.row_count("expense_report") == 2
    stats = ds.column_stats("expense_report", amount_column="Amount")
    assert stats["amount"] == 300.0


def test_non_configured_source_still_delegates_to_table_source():
    class _RecordingTableSource:
        def resolve_version(self, source):
            return "table-v1"

    ds = _ds(
        {}, {}, table_source=_RecordingTableSource(),
        bindings={"approval_aging": "cat.sch.approval_aging"},
    )
    assert ds.resolve_version("approval_aging") == "table-v1"


def test_configured_binding_takes_priority_when_an_upload_shares_the_path():
    """An upload's own recorded sha256 wins over re-reading the file, when
    the same exact path happens to be both configured AND a recorded
    upload -- the more specific, per-run provenance (the upload record)
    is checked first."""
    row = {"upload_id": "UP-1", "filename": "expense_report.csv", "volume_path": VOLUME_PATH,
           "sha256": CSV_SHA256, "status": "Ready"}

    class _PersistenceWithUpload:
        def list_uploaded_files(self, engagement_id=None):
            return [row]

    ds = VolumeUploadAwareDataSource(
        table_source=_ExplodingTableSource(),
        bindings={"expense_report": VOLUME_PATH},
        contract_sources={},
        persistence=_PersistenceWithUpload(),
        export_storage=_FakeExportStorage({VOLUME_PATH: CSV_BYTES}),
        configured_volume_paths={VOLUME_PATH: {"path": VOLUME_PATH}},
    )
    assert ds.resolve_version("expense_report") == CSV_SHA256  # matches either way here
    # But it took the upload path, not the read-and-hash path: the export
    # storage was never even asked, since resolve_version returns the
    # recorded sha256 straight from the upload row.
    assert ds.export_storage.read_calls == []


# ── resolve_source_versions concurrency (P3/P4 perf gap review 2026-09-25) ──


class _TrackingExportStorage:
    """Like `_FakeExportStorage`, but can simulate a slow read (`sleep_s`)
    and records how many `.read()` calls were in flight at once, so a test
    can assert real concurrency happened rather than a short wall time by
    coincidence."""

    def __init__(self, files: dict[str, bytes], sleep_s: float = 0.0):
        self.files = files
        self.sleep_s = sleep_s
        self.read_calls: list[str] = []
        self._lock = threading.Lock()
        self.current = 0
        self.max_seen = 0

    def read(self, path):
        with self._lock:
            self.current += 1
            self.max_seen = max(self.max_seen, self.current)
        try:
            self.read_calls.append(path)
            if self.sleep_s:
                time.sleep(self.sleep_s)
            if path not in self.files:
                raise FileNotFoundError(path)
            return self.files[path]
        finally:
            with self._lock:
                self.current -= 1


class _CountingPersistence:
    """Like `_FakePersistence`, but counts how many times
    `list_uploaded_files()` was actually called -- proves the listing is
    fetched once and shared, not once per source resolved."""

    def __init__(self, rows):
        self.rows = rows
        self.call_count = 0

    def list_uploaded_files(self, engagement_id=None):
        self.call_count += 1
        return self.rows


class _SettingsStub:
    def __init__(self, max_connections):
        self.max_connections = max_connections


class _TableSourceWithSettings:
    """A table_source stub carrying a `.settings.max_connections`, the same
    attribute path `VolumeUploadAwareDataSource._resolve_cap` reads from a
    real `UCTableDataSource`."""

    def __init__(self, max_connections, table_versions=None):
        self.settings = _SettingsStub(max_connections)
        self._table_versions = table_versions or {}

    def resolve_source_versions(self, names):
        return {n: self._table_versions.get(n, f"table-v-{n}") for n in names}


def test_resolve_source_versions_runs_configured_files_concurrently():
    n = 6
    sleep_s = 0.05
    paths = {f"p{i}": f"/Volumes/cat/sch/vol/tne/f{i}.csv" for i in range(n)}
    files = {path: CSV_BYTES for path in paths.values()}
    export_storage = _TrackingExportStorage(files, sleep_s=sleep_s)
    configured = {path: {"path": path} for path in paths.values()}

    ds = VolumeUploadAwareDataSource(
        table_source=_ExplodingTableSource(),
        bindings=paths,
        contract_sources={},
        persistence=_FakePersistence(),
        export_storage=export_storage,
        configured_volume_paths=configured,
    )

    start = time.monotonic()
    result = ds.resolve_source_versions(list(paths))
    elapsed = time.monotonic() - start

    assert result == {name: CSV_SHA256 for name in paths}
    sequential_time = n * sleep_s
    assert elapsed < sequential_time / 2, (
        f"resolve_source_versions took {elapsed:.3f}s for {n} configured files at "
        f"{sleep_s}s each ({sequential_time:.3f}s sequential) -- looks serialised"
    )
    assert export_storage.max_seen > 1, "no two configured-file reads ever overlapped"


def test_resolve_source_versions_respects_max_connections_bound():
    n = 6
    sleep_s = 0.05
    cap = 2
    paths = {f"p{i}": f"/Volumes/cat/sch/vol/tne/f{i}.csv" for i in range(n)}
    files = {path: CSV_BYTES for path in paths.values()}
    export_storage = _TrackingExportStorage(files, sleep_s=sleep_s)
    configured = {path: {"path": path} for path in paths.values()}

    ds = VolumeUploadAwareDataSource(
        table_source=_TableSourceWithSettings(max_connections=cap),
        bindings=paths,
        contract_sources={},
        persistence=_FakePersistence(),
        export_storage=export_storage,
        configured_volume_paths=configured,
    )

    ds.resolve_source_versions(list(paths))

    assert export_storage.max_seen <= cap


def test_resolve_source_versions_fetches_the_upload_list_once():
    rows = [
        {"filename": "a.csv", "volume_path": "/Volumes/cat/sch/vol/uploads/a.csv", "sha256": "sha-a"},
        {"filename": "b.csv", "volume_path": "/Volumes/cat/sch/vol/uploads/b.csv", "sha256": "sha-b"},
        {"filename": "c.csv", "volume_path": "/Volumes/cat/sch/vol/uploads/c.csv", "sha256": "sha-c"},
    ]
    persistence = _CountingPersistence(rows)
    bindings = {r["filename"][:-4]: r["volume_path"] for r in rows}

    ds = VolumeUploadAwareDataSource(
        table_source=_ExplodingTableSource(),
        bindings=bindings,
        contract_sources={},
        persistence=persistence,
        export_storage=_FakeExportStorage({}),
        configured_volume_paths={},
    )

    result = ds.resolve_source_versions(list(bindings))

    assert result == {"a": "sha-a", "b": "sha-b", "c": "sha-c"}
    assert persistence.call_count == 1, "list_uploaded_files() must be fetched once and shared"


def test_resolve_source_versions_deterministic_with_mixed_source_kinds():
    """One batch spanning all three kinds -- an uploaded file (pre-recorded
    hash, no I/O), a configured Volume file (read-and-hash), and a governed
    table (delegates to table_source) -- assembled back in the caller's own
    order, keyed by source name."""
    upload_row = {
        "filename": "claims.csv",
        "volume_path": "/Volumes/cat/sch/vol/uploads/u1/claims.csv",
        "sha256": "up-sha",
    }
    persistence = _CountingPersistence([upload_row])
    configured_path = "/Volumes/cat/sch/vol/tne/per_diem.csv"
    export_storage = _TrackingExportStorage({configured_path: CSV_BYTES})

    ds = VolumeUploadAwareDataSource(
        table_source=_TableSourceWithSettings(max_connections=6),
        bindings={
            "claims": upload_row["volume_path"],
            "per_diem_rates": configured_path,
            "expense_report": "cat.sch.expense_report",
        },
        contract_sources={},
        persistence=persistence,
        export_storage=export_storage,
        configured_volume_paths={configured_path: {"path": configured_path}},
    )

    result = ds.resolve_source_versions(["claims", "per_diem_rates", "expense_report"])

    assert result == {
        "claims": "up-sha",
        "per_diem_rates": CSV_SHA256,
        "expense_report": "table-v-expense_report",
    }
    assert list(result) == ["claims", "per_diem_rates", "expense_report"]
    assert persistence.call_count == 1


def test_resolve_source_versions_one_failure_fails_the_whole_batch():
    good_path = "/Volumes/cat/sch/vol/tne/good.csv"
    bad_path = "/Volumes/cat/sch/vol/tne/missing.csv"
    export_storage = _TrackingExportStorage({good_path: CSV_BYTES})
    bindings = {"good": good_path, "bad": bad_path}
    configured = {good_path: {"path": good_path}, bad_path: {"path": bad_path}}

    ds = VolumeUploadAwareDataSource(
        table_source=_ExplodingTableSource(),
        bindings=bindings,
        contract_sources={},
        persistence=_FakePersistence(),
        export_storage=export_storage,
        configured_volume_paths=configured,
    )

    with pytest.raises(ConfiguredSourceUnavailable):
        ds.resolve_source_versions(["good", "bad"])


def test_resolve_source_versions_single_configured_source_stays_sequential():
    """cap collapses to 1 for a single source needing I/O -- no thread pool
    spun up, same resolve_version() call path as before."""
    ds = _ds({VOLUME_PATH: CSV_BYTES}, {VOLUME_PATH: {"path": VOLUME_PATH}})
    result = ds.resolve_source_versions(["expense_report"])
    assert result == {"expense_report": CSV_SHA256}


# ── orchestrator.source_bindings ────────────────────────────────────────────


def test_load_source_bindings_returns_empty_dict_when_unset():
    assert load_source_bindings(None) == {}
    assert load_source_bindings("") == {}


def test_load_source_bindings_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="does not point at a file"):
        load_source_bindings(str(tmp_path / "nope.yaml"))


def test_load_source_bindings_yaml(tmp_path):
    p = tmp_path / "bindings.yaml"
    p.write_text(yaml.safe_dump({
        "tne_exco": {
            "expense_report": {"kind": "volume_file", "path": VOLUME_PATH, "sheet": "Sheet1"},
            "approval_aging": {"kind": "uc_table", "fqn": "cat.sch.approval_aging"},
        }
    }))
    cfg = load_source_bindings(str(p))
    assert cfg["tne_exco"]["expense_report"]["path"] == VOLUME_PATH
    assert cfg["tne_exco"]["approval_aging"]["fqn"] == "cat.sch.approval_aging"


def test_load_source_bindings_json(tmp_path):
    p = tmp_path / "bindings.json"
    p.write_text(json.dumps({
        "tne_exco": {"expense_report": {"kind": "volume_file", "path": VOLUME_PATH}}
    }))
    cfg = load_source_bindings(str(p))
    assert cfg["tne_exco"]["expense_report"]["path"] == VOLUME_PATH


def test_load_source_bindings_rejects_bad_kind(tmp_path):
    p = tmp_path / "bindings.yaml"
    p.write_text(yaml.safe_dump({"tne_exco": {"expense_report": {"kind": "guess"}}}))
    with pytest.raises(ConfigError, match="kind"):
        load_source_bindings(str(p))


def test_load_source_bindings_rejects_volume_file_without_path(tmp_path):
    p = tmp_path / "bindings.yaml"
    p.write_text(yaml.safe_dump({"tne_exco": {"expense_report": {"kind": "volume_file"}}}))
    with pytest.raises(ConfigError, match="requires an exact 'path'"):
        load_source_bindings(str(p))


def test_load_source_bindings_rejects_uc_table_without_fqn(tmp_path):
    p = tmp_path / "bindings.yaml"
    p.write_text(yaml.safe_dump({"tne_exco": {"approval_aging": {"kind": "uc_table"}}}))
    with pytest.raises(ConfigError, match="requires an exact 'fqn'"):
        load_source_bindings(str(p))


def test_load_source_bindings_rejects_malformed_yaml(tmp_path):
    p = tmp_path / "bindings.yaml"
    p.write_text("not: valid: yaml: [")
    with pytest.raises(ConfigError, match="could not be parsed"):
        load_source_bindings(str(p))


def test_bindings_for_skill_and_helpers():
    cfg = {
        "tne_exco": {
            "expense_report": {"kind": "volume_file", "path": VOLUME_PATH, "sheet": "Sheet1"},
            "approval_aging": {"kind": "uc_table", "fqn": "cat.sch.approval_aging"},
        }
    }
    bindings = bindings_for_skill(cfg, "tne_exco")
    assert set(bindings) == {"expense_report", "approval_aging"}
    assert bindings_for_skill(cfg, "unknown_skill") == {}

    values = suggested_values(bindings)
    assert values == {"expense_report": VOLUME_PATH, "approval_aging": "cat.sch.approval_aging"}

    paths = volume_file_paths(bindings)
    assert paths == {VOLUME_PATH: bindings["expense_report"]}


# ── integration: orchestrator.service picks up SOURCE_BINDINGS ─────────────


def test_service_suggest_bindings_uses_configured_bindings_with_no_new_ui(tmp_path):
    """Independent review item 1: "configured bindings are used by the
    existing auto-bind so the landing page needs no new UI" -- a configured
    source wins over the ordinary auto-bind rule, and an unconfigured source
    in the same Skill still resolves the normal way."""
    from orchestrator import service

    bindings_path = tmp_path / "source_bindings.yaml"
    bindings_path.write_text(yaml.safe_dump({
        "SKILL-MINI": {
            "claims": {"kind": "volume_file", "path": "/Volumes/cat/sch/vol/tne/claims_override.csv"},
        }
    }))

    from tests.test_p3_nodes import MINI_SKILL_DIR, _write_mini_data

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_mini_data(data_dir)

    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(data_dir),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
        "SKILLS_DIR": str(MINI_SKILL_DIR.parent),
        "SOURCE_BINDINGS": str(bindings_path),
    }
    ctx = service.build_app_context(env)
    suggested = service.suggest_bindings(ctx, "SKILL-MINI")
    assert suggested["claims"] == "/Volumes/cat/sch/vol/tne/claims_override.csv"
    assert suggested["register"] == "register.csv"  # unconfigured source: ordinary local auto-bind


def test_configured_bindings_for_skill_empty_when_source_bindings_unset(tmp_path):
    from orchestrator import service
    from tests.test_p3_nodes import MINI_SKILL_DIR, _write_mini_data

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_mini_data(data_dir)
    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(data_dir),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
        "SKILLS_DIR": str(MINI_SKILL_DIR.parent),
    }
    ctx = service.build_app_context(env)
    assert service.configured_bindings_for_skill(ctx, "SKILL-MINI") == {}
