"""orchestrator/adapters/export_storage.py's VolumeExportStorage client caching
(CLAUDE.md build brief P4 perf fix): one AppContext/NodeContext shares a single
adapter instance for a whole run, and orchestrator/frames.py now calls
write()/read() once per contract source (up to 8 times) rather than once per
run -- constructing a fresh WorkspaceClient on every call would re-pay SDK
auth/config resolution 8x per run. Uses an injected `workspace_client_factory`
so this needs no real workspace or network access."""

from __future__ import annotations

from orchestrator.adapters.export_storage import VolumeExportStorage


class _FakeFiles:
    def __init__(self):
        self.uploads: list[str] = []

    def upload(self, path, content, overwrite=True):
        self.uploads.append(path)

    def get_status(self, path):
        if path not in self.uploads:
            raise FileNotFoundError(path)


class _FakeClient:
    def __init__(self):
        self.files = _FakeFiles()


def test_client_is_built_once_and_reused_across_calls():
    build_count = {"n": 0}

    def factory():
        build_count["n"] += 1
        return _FakeClient()

    storage = VolumeExportStorage(volume_root="/Volumes/cat/schema/vol", workspace_client_factory=factory)

    storage.write("runs/r1/frames/a.parquet", b"a")
    storage.write("runs/r1/frames/b.parquet", b"b")
    storage.exists("runs/r1/frames/a.parquet")
    storage.exists("runs/r1/frames/c.parquet")

    assert build_count["n"] == 1


def test_default_client_is_also_cached(monkeypatch):
    import orchestrator.adapters.export_storage as export_storage_module

    build_count = {"n": 0}

    class _CountingWorkspaceClient(_FakeClient):
        def __init__(self):
            super().__init__()
            build_count["n"] += 1

    fake_sdk = type("m", (), {"WorkspaceClient": _CountingWorkspaceClient})
    monkeypatch.setitem(__import__("sys").modules, "databricks.sdk", fake_sdk)

    storage = export_storage_module.VolumeExportStorage(volume_root="/Volumes/cat/schema/vol")
    storage.write("runs/r1/frames/a.parquet", b"a")
    storage.write("runs/r1/frames/b.parquet", b"b")
    storage.write("runs/r1/frames/c.parquet", b"c")

    assert build_count["n"] == 1
