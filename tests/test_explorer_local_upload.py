"""orchestrator.service._resolve_explorer_sources / _build_explorer_data_source
on the local backend: an Explorer source ticked from the checklist (CLAUDE.md
§6 D5a) with `kind == "upload"` must be resolved from the uploaded file's OWN
recorded volume_path/sha256 (service.upload_file + LocalPersistence), never
treated as a file under ctx.local_data_root the way a `local_file` source is
-- the bug this file's tests close. Every test goes through the real local
upload path (service.upload_file, LocalPersistence) and the real
start_explorer_run/ThreadExecutor pipeline, never a stub DataSourceAdapter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator import service
from orchestrator.errors import ExplorerInputError
from tests.test_explorer_service import AUDIT_PERIOD, _build_ctx, _wait_for

UPLOAD_CSV = b"Employee ID,Amount\n1,100\n2,200\n3,300\n"


def _upload_ready_csv(ctx: service.AppContext, *, content: bytes = UPLOAD_CSV, filename: str = "claims.csv") -> dict:
    row = service.upload_file(ctx, filename=filename, content=content, uploaded_by="alice")
    assert row["status"] == "Ready", row
    return row


def test_local_backend_upload_source_profiles_alongside_a_governed_file(tmp_path):
    """A local-backend Explorer run with one governed CSV (`local_file`) and
    one Ready upload (`upload`) profiles BOTH with the right row counts, and
    the upload's own recorded hash lands in the run fingerprint -- not the
    upload_id, and not read from (or through) ctx.local_data_root."""
    ctx = _build_ctx(tmp_path, model_sonnet=None)
    ctx.executor.start()
    try:
        upload_row = _upload_ready_csv(ctx)
        sources = [
            {"kind": "local_file", "ref": "expense_report.csv"},
            {"kind": "upload", "ref": upload_row["upload_id"], "format": "csv"},
        ]
        run_id = service.start_explorer_run(
            ctx, objective="Assess claims across governed data and an ad-hoc upload",
            sources=sources, audit_period=AUDIT_PERIOD, run_owner="alice",
        )
        status = _wait_for(ctx, run_id, {"awaiting_confirmation", "failed"})
        assert status == "awaiting_confirmation", service.get_run(ctx, run_id).get("status_reason")

        state = ctx.persistence.load_state(run_id)
        governed_asset = next(b for b in state.data_assets if b["kind"] == "local_file")
        upload_asset = next(b for b in state.data_assets if b["kind"] == "upload")

        # The bug: the local branch used to resolve an `upload` entry's ref
        # (the upload_id) as a file under local_data_root. Fixed: table_fqn
        # is the upload's OWN recorded volume_path, version its own sha256.
        assert upload_asset["table_fqn"] == upload_row["volume_path"]
        assert upload_asset["table_fqn"] != upload_row["upload_id"]
        assert upload_asset["version"] == upload_row["sha256"]

        sources_profiled = state.profile_result["sources"]
        assert sources_profiled[governed_asset["source"]]["row_count"] == 5
        assert sources_profiled[upload_asset["source"]]["row_count"] == 3

        fingerprint = ctx.persistence.get_fingerprint(state.fingerprint_id)
        recorded_hashes = json.loads(fingerprint["uploaded_file_hashes"])
        assert recorded_hashes[upload_row["volume_path"]] == upload_row["sha256"]
    finally:
        ctx.executor.stop()


def test_local_backend_non_ready_upload_source_raises(tmp_path):
    """An upload that has not reached Ready (still Profiling, or Failed)
    fails loudly at start_explorer_run -- never silently read as if it were
    a governed file under local_data_root (CLAUDE.md NN14)."""
    ctx = _build_ctx(tmp_path, model_sonnet=None)
    upload_row = _upload_ready_csv(ctx)
    ctx.persistence.update_uploaded_file(upload_row["upload_id"], status="Profiling")

    with pytest.raises(ExplorerInputError, match="not a Ready uploaded file"):
        service.start_explorer_run(
            ctx, objective="Assess claims",
            sources=[{"kind": "upload", "ref": upload_row["upload_id"], "format": "csv"}],
            audit_period=AUDIT_PERIOD, run_owner="alice",
        )


def test_local_backend_tampered_upload_fails_loudly_on_hash_mismatch(tmp_path):
    """The uploaded_files row records the sha256 at upload time; if the
    bytes on disk change afterwards, the run must fail loudly (here, the
    `discover` node's own re-resolve-and-compare against the pinned
    version, a ContractViolation) -- never silently accept the tampered
    content (CLAUDE.md NN14)."""
    ctx = _build_ctx(tmp_path, model_sonnet=None)
    ctx.executor.start()
    try:
        upload_row = _upload_ready_csv(ctx)
        # Tamper with the bytes on disk AFTER upload -- the persisted
        # uploaded_files row still records the ORIGINAL sha256.
        Path(upload_row["volume_path"]).write_bytes(b"Employee ID,Amount\n9,999\n")

        run_id = service.start_explorer_run(
            ctx, objective="Assess claims",
            sources=[{"kind": "upload", "ref": upload_row["upload_id"], "format": "csv"}],
            audit_period=AUDIT_PERIOD, run_owner="alice",
        )
        status = _wait_for(ctx, run_id, {"awaiting_confirmation", "failed"})
        assert status == "failed", service.get_run(ctx, run_id).get("status_reason")
        reason = service.get_run(ctx, run_id).get("status_reason") or ""
        assert "resolves to" in reason and "changed after it was pinned" in reason
    finally:
        ctx.executor.stop()
