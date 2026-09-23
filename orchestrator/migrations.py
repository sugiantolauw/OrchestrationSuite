from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from orchestrator.errors import MigrationError

_FILENAME_RE = re.compile(r"^(\d+)_.*\.sql$")


@dataclass(frozen=True)
class MigrationFile:
    version: str
    description: str
    path: Path
    checksum: str
    sql: str


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def discover_migrations(ddl_dir: Path) -> list[MigrationFile]:
    files = sorted(p for p in Path(ddl_dir).glob("*.sql") if _FILENAME_RE.match(p.name))
    migrations = []
    for p in files:
        m = _FILENAME_RE.match(p.name)
        version = m.group(1)
        description = p.stem[len(version) + 1 :]
        sql = p.read_text()
        migrations.append(
            MigrationFile(version=version, description=description, path=p, checksum=_checksum(sql), sql=sql)
        )
    return migrations


def split_statements(sql: str) -> list[str]:
    statements = []
    for raw in sql.split(";"):
        stmt = raw.strip()
        if not stmt:
            continue
        # drop lines that are pure comments so an all-comment fragment isn't sent to the engine
        lines = [ln for ln in stmt.splitlines() if ln.strip() and not ln.strip().startswith("--")]
        if lines:
            statements.append(stmt)
    return statements


def plan_migrations(
    ddl_dir: Path, applied: dict[str, str]
) -> tuple[list[MigrationFile], list[MigrationFile]]:
    """Returns (pending, already_applied) given {version: checksum} of applied migrations."""
    discovered = discover_migrations(ddl_dir)
    pending = []
    already = []
    for m in discovered:
        if m.version in applied:
            if applied[m.version] != m.checksum:
                raise MigrationError(
                    f"migration {m.version} ({m.path.name}) checksum changed on disk: "
                    f"applied={applied[m.version]} current={m.checksum}"
                )
            already.append(m)
        else:
            pending.append(m)
    return pending, already
