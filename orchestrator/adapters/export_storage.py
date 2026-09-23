"""ExportStorageAdapter (orchestrator/adapters/protocols.py) implementations:
LocalExportStorage for tests and ORCH_BACKEND=local runs, VolumeExportStorage
for the deployed App writing to the Databricks Volume (CLAUDE.md §7 -- Volumes
through the Files API; DBX_VOLUME comes from config, never a hardcoded path,
§3 non-negotiable 16 / §0.2's `_UPLOAD_BASE` defect this does not repeat)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class LocalExportStorage:
    root_dir: str | Path

    def _path(self, path: str) -> Path:
        return Path(self.root_dir) / path

    def write(self, path: str, content: bytes) -> str:
        full = self._path(path)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(content)
        return str(full)

    def read(self, path: str) -> bytes:
        return self._path(path).read_bytes()

    def exists(self, path: str) -> bool:
        return self._path(path).is_file()


@dataclass
class VolumeExportStorage:
    """Writes/reads under `<DBX_VOLUME>/<path>` via
    WorkspaceClient().files.upload/download (CLAUDE.md §7). `volume_root` is a
    Unity Catalog Volume path (e.g. /Volumes/<catalog>/<schema>/<volume>),
    supplied by orchestrator.config -- never a literal in this file."""

    volume_root: str
    workspace_client_factory: object = None  # Callable[[], WorkspaceClient] | None

    def _client(self):
        if self.workspace_client_factory is not None:
            return self.workspace_client_factory()
        from databricks.sdk import WorkspaceClient

        return WorkspaceClient()

    def _full_path(self, path: str) -> str:
        return f"{self.volume_root.rstrip('/')}/{path.lstrip('/')}"

    def write(self, path: str, content: bytes) -> str:
        import io

        full = self._full_path(path)
        self._client().files.upload(full, io.BytesIO(content), overwrite=True)
        return full

    def read(self, path: str) -> bytes:
        full = self._full_path(path) if not path.startswith(self.volume_root) else path
        resp = self._client().files.download(full)
        return resp.contents.read()

    def exists(self, path: str) -> bool:
        full = self._full_path(path) if not path.startswith(self.volume_root) else path
        try:
            self._client().files.get_status(full)
            return True
        except Exception:
            return False
