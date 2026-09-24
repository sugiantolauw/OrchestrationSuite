"""orchestrator.service.upload_file's handling of untrusted filenames and
export integrity (independent review 2026-09-24, item 4):

  * a filename containing `../` or an absolute path cannot escape
    uploads/<upload_id>/ -- Path(filename).name plus a character allow-list;
  * an upload never overwrites an existing object;
  * get_export raises if the bytes read back do not match the recorded
    sha256, rather than silently serving mismatched content.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from orchestrator import service
from tests.test_p3_nodes import MINI_SKILL_DIR, _write_mini_data


def _build_ctx(tmp_path: Path) -> service.AppContext:
    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(tmp_path / "data"),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
        "SKILLS_DIR": str(MINI_SKILL_DIR.parent),
        "CODE_REVISION": "test-fixed-revision",
    }
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    _write_mini_data(data_dir)
    return service.build_app_context(env)


@pytest.mark.parametrize("malicious_name", ["../../etc/passwd", "../../../secrets.csv", "/etc/passwd"])
def test_posix_traversal_filename_is_stripped_to_a_plain_name(tmp_path, malicious_name):
    ctx = _build_ctx(tmp_path)
    content = b"Employee ID,Amount\n1,10\n"
    row = service.upload_file(ctx, filename=malicious_name, content=content, uploaded_by="alice")

    export_root = Path(ctx.export_storage.root_dir).resolve()
    written = Path(row["volume_path"]).resolve()
    assert export_root in written.parents, f"{written} escaped the upload root {export_root}"
    # Path(...).name drops every directory component -- the written file is
    # always a plain, single-segment name directly under uploads/<id>/.
    assert written.parent.name.startswith("UP-")
    assert "/" not in written.name and written.name not in ("..", ".")


def test_backslash_in_a_filename_is_rejected_outright(tmp_path):
    # POSIX Path() does not treat '\' as a separator, so it survives into
    # the character allow-list check and is rejected there instead --
    # belt-and-suspenders, but still never silently written anywhere.
    ctx = _build_ctx(tmp_path)
    with pytest.raises(ValueError):
        service.upload_file(ctx, filename="..\\..\\windows\\win.ini", content=b"x", uploaded_by="alice")


def test_filename_with_disallowed_characters_is_rejected(tmp_path):
    ctx = _build_ctx(tmp_path)
    with pytest.raises(ValueError):
        service.upload_file(ctx, filename="claims;rm -rf.csv", content=b"x", uploaded_by="alice")


def test_upload_never_overwrites_an_existing_object(tmp_path, monkeypatch):
    ctx = _build_ctx(tmp_path)

    class _FixedUUID:
        hex = "deadbeefcafe" + "0" * 20

    # Force every upload_id in this test to collide, so the second upload's
    # destination path is identical to the first's.
    monkeypatch.setattr("uuid.uuid4", lambda: _FixedUUID())

    service.upload_file(ctx, filename="claims.csv", content=b"a", uploaded_by="alice")
    with pytest.raises(ValueError, match="already exists"):
        service.upload_file(ctx, filename="claims.csv", content=b"b", uploaded_by="alice")


def test_get_export_raises_on_sha256_mismatch(tmp_path):
    ctx = _build_ctx(tmp_path)
    path = ctx.export_storage.write("runs/R1/report.xlsx", b"original content")
    ctx.persistence.record_export(
        "R1", "xlsx", path=path, sha256=hashlib.sha256(b"original content").hexdigest(),
        created_by="alice", now="2026-01-01T00:00:00Z",
    )

    # Correct content reads back cleanly.
    filename, content = service.get_export(ctx, "R1", "xlsx")
    assert content == b"original content"

    # Corrupt the stored bytes after the fact (e.g. a partial write, or
    # tampering) -- get_export must not silently serve them.
    Path(path).write_bytes(b"tampered content")
    with pytest.raises(ValueError, match="integrity check"):
        service.get_export(ctx, "R1", "xlsx")
