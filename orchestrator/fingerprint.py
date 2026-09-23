from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from orchestrator.config import Settings, endpoint_config, runtime_config_hash
from orchestrator.errors import ConfigError, FingerprintMismatch

_REPO_ROOT = Path(__file__).resolve().parent.parent

_SKILL_FILES = ("manifest.yaml", "contract.yaml", "plan.yaml", "thresholds.yaml", "findings.yaml")

# Every field of run_fingerprints except fingerprint_id (computed) and created_at
# (recorded but excluded from the hash, so identical setups share a fingerprint_id).
_HASHED_FIELDS = (
    "source_table_versions",
    "uploaded_file_hashes",
    "skill_content_hash",
    "code_revision",
    "dependency_lock_hash",
    "runtime_config_hash",
    "endpoint_config",
    "prompt_template_version",
)


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_entries(entries: list[tuple[str, bytes]]) -> str:
    h = hashlib.sha256()
    for rel_path, content in sorted(entries):
        h.update(rel_path.encode("utf-8"))
        h.update(b"\0")
        h.update(content)
        h.update(b"\0")
    return h.hexdigest()


def _skill_content_hash(skill_dir: Path | None) -> str | None:
    if skill_dir is None:
        return None
    skill_dir = Path(skill_dir)
    entries: list[tuple[str, bytes]] = []
    for name in _SKILL_FILES:
        p = skill_dir / name
        if p.is_file():
            entries.append((name, p.read_bytes()))
    prompts_dir = skill_dir / "prompts"
    if prompts_dir.is_dir():
        for p in prompts_dir.rglob("*"):
            if p.is_file():
                entries.append((str(p.relative_to(skill_dir)), p.read_bytes()))
    return _hash_entries(entries)


def _prompt_template_version(prompts_dirs: list[Path]) -> str:
    entries: list[tuple[str, bytes]] = []
    for d in prompts_dirs or []:
        d = Path(d)
        if d.is_dir():
            for p in d.rglob("*"):
                if p.is_file():
                    entries.append((str(p.relative_to(d)), p.read_bytes()))
    if not entries:
        # Explicit sentinel: P1A has no prompts yet (no LLM calls). Never a silent default.
        return "none"
    return _hash_entries(entries)


def _resolve_code_revision(explicit: str | None, settings_value: str | None) -> str:
    if explicit:
        return explicit
    if settings_value:
        return settings_value
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise ConfigError(
            "code_revision could not be resolved: no explicit value, no settings.code_revision, "
            f"and git is unavailable ({exc})"
        ) from exc
    if not rev:
        raise ConfigError("code_revision could not be resolved: git rev-parse HEAD returned empty")
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if status.strip():
        rev += "+dirty"
    return rev


def compute_fingerprint(
    *,
    settings: Settings,
    source_table_versions: dict,
    uploaded_file_hashes: dict,
    skill_dir: Path | None,
    requirements_path: Path,
    prompts_dirs: list[Path],
    code_revision: str | None = None,
) -> dict:
    requirements_path = Path(requirements_path)
    fields = {
        "source_table_versions": _canonical_json(source_table_versions),
        "uploaded_file_hashes": _canonical_json(uploaded_file_hashes),
        "skill_content_hash": _skill_content_hash(skill_dir),
        "code_revision": _resolve_code_revision(code_revision, settings.code_revision),
        "dependency_lock_hash": sha256_bytes(requirements_path.read_bytes()),
        "runtime_config_hash": runtime_config_hash(settings),
        "endpoint_config": _canonical_json(endpoint_config(settings)),
        "prompt_template_version": _prompt_template_version(prompts_dirs),
    }
    fingerprint_id = hashlib.sha256(
        _canonical_json({k: fields[k] for k in _HASHED_FIELDS}).encode("utf-8")
    ).hexdigest()

    result = dict(fields)
    result["fingerprint_id"] = fingerprint_id
    result["created_at"] = datetime.now(timezone.utc).isoformat()
    return result


def verify_fingerprint(stored: dict, current: dict) -> None:
    differing = {}
    for field in _HASHED_FIELDS:
        if stored.get(field) != current.get(field):
            differing[field] = {"stored": stored.get(field), "current": current.get(field)}
    if stored.get("fingerprint_id") != current.get("fingerprint_id"):
        differing.setdefault(
            "fingerprint_id",
            {"stored": stored.get("fingerprint_id"), "current": current.get("fingerprint_id")},
        )
    if differing:
        raise FingerprintMismatch(differing)
