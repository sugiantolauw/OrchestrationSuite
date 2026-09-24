"""Uploaded files as run sources on the UC backend (CLAUDE.md §5 UI item 5):
a contract source bound to an uploaded file's Volume path (rather than a UC
table FQN) is read from the Volume via the Files API, its sha256 verified
against the value recorded at upload time before use, and parsed through the
same contract-driven logic LocalFileDataSource uses -- never sent through
the UC table SQL path, which cannot make sense of a Volume path.

Unit tests exercise orchestrator.adapters.datasource_volume_upload.
VolumeUploadAwareDataSource directly with fakes (no network). The
integration test exercises the real orchestrator.service wiring
(build_app_context -> _uc_factory) with a fake Files client and a fake
UCTableDataSource, so the production code path -- not a reimplementation of
it -- is what's actually under test."""

from __future__ import annotations

import hashlib

import pandas as pd
import pytest

from orchestrator.adapters.datasource_volume_upload import VolumeUploadAwareDataSource
from orchestrator.contract import SourceVersionMismatch

CSV_BYTES = b"Employee ID,Amount\n1,100\n2,200\n"
CSV_SHA256 = hashlib.sha256(CSV_BYTES).hexdigest()


class _ExplodingTableSource:
    """Standing in for UCTableDataSource: any call proves an uploaded-file-
    bound source was wrongly sent through the SQL path."""

    def resolve_version(self, source):
        raise AssertionError(f"table_source.resolve_version called for {source!r}")

    def read_population(self, source, **kwargs):
        raise AssertionError(f"table_source.read_population called for {source!r}")

    def row_count(self, source, **kwargs):
        raise AssertionError(f"table_source.row_count called for {source!r}")

    def column_stats(self, source, **kwargs):
        raise AssertionError(f"table_source.column_stats called for {source!r}")


class _FakePersistence:
    def __init__(self, uploaded_files):
        self._uploaded_files = uploaded_files

    def list_uploaded_files(self, engagement_id=None):
        return self._uploaded_files


class _FakeExportStorage:
    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.read_calls: list[str] = []

    def read(self, path):
        self.read_calls.append(path)
        return self.files[path]


UPLOAD_ROW = {
    "upload_id": "UP-1", "filename": "claims.csv",
    "volume_path": "/Volumes/cat/sch/vol/uploads/UP-1/claims.csv",
    "sha256": CSV_SHA256, "status": "Ready",
}


def _ds(uploaded_files=(UPLOAD_ROW,), files=None, bindings=None, contract_sources=None, table_source=None):
    files = files if files is not None else {UPLOAD_ROW["volume_path"]: CSV_BYTES}
    bindings = bindings if bindings is not None else {"claims": UPLOAD_ROW["volume_path"]}
    return VolumeUploadAwareDataSource(
        table_source=table_source or _ExplodingTableSource(),
        bindings=bindings,
        contract_sources=contract_sources or {},
        persistence=_FakePersistence(list(uploaded_files)),
        export_storage=_FakeExportStorage(files),
    )


# ── uploaded-file-bound source ───────────────────────────────────────────────


def test_resolve_version_returns_the_recorded_sha256():
    ds = _ds()
    assert ds.resolve_version("claims") == CSV_SHA256


def test_read_population_reads_via_export_storage_and_verifies_sha256():
    ds = _ds()
    df = ds.read_population("claims", version=CSV_SHA256)
    assert list(df["Employee ID"]) == [1, 2]
    assert list(df["Amount"]) == [100, 200]
    assert "__source" in df.columns and "__row_key" in df.columns
    assert ds.export_storage.read_calls == [UPLOAD_ROW["volume_path"]]


def test_read_population_rejects_a_stale_pinned_version():
    """CLAUDE.md §4.1 TOCTOU ordering: the version passed in must still
    match what was recorded before any byte is fetched."""
    ds = _ds()
    with pytest.raises(SourceVersionMismatch):
        ds.read_population("claims", version="not-the-real-hash")
    assert ds.export_storage.read_calls == []  # never even fetched


def test_read_population_rejects_content_that_no_longer_matches_the_recorded_hash():
    """The file in the Volume changed (or was never what uploaded_files
    says) since it was recorded -- verified against the ACTUAL bytes read,
    not just trusted because the pinned version matched."""
    tampered_files = {UPLOAD_ROW["volume_path"]: b"Employee ID,Amount\n9,999\n"}
    ds = _ds(files=tampered_files)
    with pytest.raises(SourceVersionMismatch):
        ds.read_population("claims", version=CSV_SHA256)


def test_row_count_and_column_stats_go_through_the_upload_path_too():
    ds = _ds()
    assert ds.row_count("claims", version=CSV_SHA256) == 2
    stats = ds.column_stats("claims", version=CSV_SHA256, amount_column="Amount")
    assert stats["amount"] == 300.0


def test_format_inferred_from_uploaded_filename_when_contract_silent():
    xlsx_bytes = _xlsx_bytes({"Employee ID": [1], "Amount": [50]})
    row = {**UPLOAD_ROW, "filename": "claims.xlsx", "volume_path": "/Volumes/cat/sch/vol/uploads/UP-2/claims.xlsx",
           "sha256": hashlib.sha256(xlsx_bytes).hexdigest()}
    ds = _ds(
        uploaded_files=(row,), files={row["volume_path"]: xlsx_bytes},
        bindings={"claims": row["volume_path"]},
    )
    df = ds.read_population("claims", version=row["sha256"])
    assert list(df["Amount"]) == [50]


def test_contract_declared_format_and_header_trim_still_applied():
    """"applies the same contract validation" (CLAUDE.md §5 UI item 5) --
    header_trim from contract.yaml is honoured for an uploaded file exactly
    as it is for a local one."""
    csv_bytes = b"Employee ID , Amount \n1,100\n"
    row = {**UPLOAD_ROW, "sha256": hashlib.sha256(csv_bytes).hexdigest()}
    ds = _ds(
        uploaded_files=(row,), files={UPLOAD_ROW["volume_path"]: csv_bytes},
        contract_sources={"claims": {"format": "csv", "header_trim": True}},
    )
    df = ds.read_population("claims", version=row["sha256"])
    assert "Employee ID" in df.columns  # trimmed, not "Employee ID "


def _xlsx_bytes(data: dict) -> bytes:
    import io

    buf = io.BytesIO()
    pd.DataFrame(data).to_excel(buf, index=False)
    return buf.getvalue()


# ── non-upload source: unchanged, delegates straight to table_source ────────


def test_non_upload_source_delegates_to_table_source_unchanged():
    calls = []

    class _RecordingTableSource:
        def resolve_version(self, source):
            calls.append(("resolve_version", source))
            return "v7"

        def read_population(self, source, **kwargs):
            calls.append(("read_population", source))
            return pd.DataFrame({"x": [1]})

        def row_count(self, source, **kwargs):
            calls.append(("row_count", source))
            return 42

        def column_stats(self, source, **kwargs):
            calls.append(("column_stats", source))
            return {"amount": 1.0, "min_date": None, "max_date": None}

    ds = _ds(bindings={"expense_report": "cat.sch.expense_report"}, table_source=_RecordingTableSource())
    assert ds.resolve_version("expense_report") == "v7"
    assert len(ds.read_population("expense_report", version="v7")) == 1
    assert ds.row_count("expense_report", version="v7") == 42
    assert ds.column_stats("expense_report", version="v7") == {"amount": 1.0, "min_date": None, "max_date": None}
    assert [c[0] for c in calls] == ["resolve_version", "read_population", "row_count", "column_stats"]


def test_mixed_bindings_route_each_source_independently():
    """One Skill run can bind SOME sources to UC tables and others to
    uploaded files at once -- each must go to the right place."""

    class _RecordingTableSource:
        def resolve_version(self, source):
            return "table-v1"

    ds = _ds(
        bindings={"claims": UPLOAD_ROW["volume_path"], "attendee_validity": "cat.sch.attendee_validity"},
        table_source=_RecordingTableSource(),
    )
    assert ds.resolve_version("claims") == CSV_SHA256
    assert ds.resolve_version("attendee_validity") == "table-v1"


# ── integration: orchestrator.service's real UC wiring, fake Files client ───


class _FakeFiles:
    def __init__(self, blobs: dict[str, bytes]):
        self.blobs = blobs
        self.download_calls: list[str] = []

    def upload(self, path, content, overwrite=True):
        self.blobs[path] = content.read() if hasattr(content, "read") else content

    def download(self, path):
        self.download_calls.append(path)

        class _Resp:
            def __init__(self, data):
                self.contents = _Stream(data)

        class _Stream:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

        return _Resp(self.blobs[path])

    def get_status(self, path):
        if path not in self.blobs:
            raise FileNotFoundError(path)


class _FakeWorkspaceClient:
    def __init__(self, files: _FakeFiles, **kwargs):
        self.files = files


class _FakePersistenceForBuild:
    """Stands in for DeltaPersistence: no real SQL warehouse -- migrate() is
    a no-op, list_uploaded_files() serves the one row this test cares
    about."""

    def __init__(self, settings):
        pass

    def migrate(self):
        pass

    def list_uploaded_files(self, engagement_id=None):
        return [UPLOAD_ROW]


class _StubUCTableDataSource:
    def __init__(self, settings, bindings):
        self.settings = settings
        self.bindings = bindings

    def resolve_version(self, source):
        raise AssertionError(f"UCTableDataSource.resolve_version called for {source!r} -- should have been routed to the upload path")


def test_service_uc_factory_routes_an_uploaded_binding_through_the_volume_not_sql(monkeypatch):
    import orchestrator.adapters.datasource_uc as datasource_uc_module
    import orchestrator.service as service

    fake_files = _FakeFiles({UPLOAD_ROW["volume_path"]: CSV_BYTES})
    monkeypatch.setattr(service, "DeltaPersistence", _FakePersistenceForBuild)
    monkeypatch.setattr(datasource_uc_module, "UCTableDataSource", _StubUCTableDataSource)
    import databricks.sdk as sdk_module

    monkeypatch.setattr(sdk_module, "WorkspaceClient", lambda **kw: _FakeWorkspaceClient(fake_files))

    env = {
        "DBX_CATALOG": "cat", "DBX_SCHEMA": "sch", "DBX_VOLUME": "/Volumes/cat/sch/vol",
        "DBX_WAREHOUSE_HTTP_PATH": "/sql/1.0/warehouses/abc",
        "DATABRICKS_HOST": "https://x.cloud.databricks.com",
    }
    ctx = service.build_app_context(env)
    assert ctx.backend == "uc"

    bindings = {"claims": UPLOAD_ROW["volume_path"]}
    contract_sources = {"claims": {"format": "csv"}}
    data_source = ctx.data_source_factory(bindings, contract_sources)

    version = data_source.resolve_version("claims")
    assert version == CSV_SHA256
    df = data_source.read_population("claims", version=version)
    assert list(df["Amount"]) == [100, 200]
    assert fake_files.download_calls == [UPLOAD_ROW["volume_path"]]
