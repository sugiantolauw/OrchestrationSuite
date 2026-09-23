from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from orchestrator.config import Settings, endpoint_config, runtime_config_hash
from orchestrator.errors import ConfigError, FingerprintMismatch

_REPO_ROOT = Path(__file__).resolve().parent.parent

_SKILL_FILES = (
    "manifest.yaml",
    "contract.yaml",
    "plan.yaml",
    "thresholds.yaml",
    "findings.yaml",
    # N10: risk_control.yaml (the risk/control register plan.yaml's control_id/
    # risk_id reference) and catalogue.yaml (the UI's Test Catalogue / PPTX
    # methodology appendix text, G8-checked against thresholds.yaml) are both
    # Skill content -- a change to either must change the run fingerprint.
    "risk_control.yaml",
    "catalogue.yaml",
)

# Every field of run_fingerprints except fingerprint_id (computed) and created_at
# (recorded but excluded from the hash, so identical setups share a fingerprint_id).
_HASHED_FIELDS = (
    "source_table_versions",
    "uploaded_file_hashes",
    "reference_data_hashes",
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


def skill_content_entries(skill_dir: Path | None) -> list[tuple[str, bytes]]:
    """Every file that is part of a Skill's CONTENT (CLAUDE.md §3 NN7/NN8,
    P2/P3 gate review item 9) -- {relative path: raw bytes}, as a list of
    (path, content) pairs. The SINGLE enumeration both `_skill_content_hash`
    (below, the run fingerprint) and orchestrator.skill_registry.register_skill
    (the skill_versions row) use, so the two can never drift: whatever this
    lists is exactly what is hashed AND exactly what is stored, which is what
    makes "re-hashing the stored content reproduces skill_content_hash" true
    by construction rather than by two independently-maintained file lists
    happening to agree.

    manifest/contract/plan/thresholds/findings/risk_control/catalogue (all of
    `_SKILL_FILES`), custom.py and workspace.py (the Skill protocol's own
    two escape-hatch/render modules, CLAUDE.md §4.4), and every file under
    prompts/ and reference/ (Skill-authored prompt overrides and reference
    lookup tables) -- a change to any of these must change both the run
    fingerprint and what a past run's ledger entry can reconstruct, just as a
    plan.yaml edit already does."""
    if skill_dir is None:
        return []
    skill_dir = Path(skill_dir)
    entries: list[tuple[str, bytes]] = []
    for name in _SKILL_FILES:
        p = skill_dir / name
        if p.is_file():
            entries.append((name, p.read_bytes()))
    for name in ("custom.py", "workspace.py"):
        p = skill_dir / name
        if p.is_file():
            entries.append((name, p.read_bytes()))
    for dirname in ("prompts", "reference"):
        d = skill_dir / dirname
        if d.is_dir():
            for p in d.rglob("*"):
                if p.is_file():
                    entries.append((str(p.relative_to(skill_dir)), p.read_bytes()))
    return entries


def _skill_content_hash(skill_dir: Path | None) -> str | None:
    if skill_dir is None:
        return None
    return _hash_entries(skill_content_entries(skill_dir))


def skill_content_hash(skill_dir: Path | None) -> str | None:
    """Public entry point for orchestrator.skills -- reuses the same hashing the run
    fingerprint uses (CLAUDE.md P2a) rather than duplicating it."""
    return _skill_content_hash(skill_dir)


def hash_skill_content_entries(entries: list[tuple[str, bytes]]) -> str:
    """Public wrapper over the same hashing skill_content_hash uses, over an
    already-built {path: bytes} entry list rather than a directory -- lets a
    caller verify a STORED skill_versions.content snapshot re-hashes to the
    same skill_content_hash it was recorded with (CLAUDE.md P2/P3 gate review
    item 9), without re-reading the Skill's files from disk (which may no
    longer exist at that version by the time the check runs)."""
    return _hash_entries(entries)


def _reference_data_hashes(reference_files: list[Path] | None) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for raw in reference_files or []:
        p = Path(raw)
        try:
            rel = str(p.resolve().relative_to(_REPO_ROOT))
        except ValueError:
            rel = str(p)
        hashes[rel] = sha256_file(p)
    return hashes


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
    reference_files: list[Path] | None = None,
    code_revision: str | None = None,
    dependency_lock_path: Path | None = None,
) -> dict:
    requirements_path = Path(requirements_path)
    # P2/P3 gate review item 8: `dependency_lock_hash` hashes a REAL lock --
    # the transitive closure of requirements.txt, pinned exactly
    # (scripts/generate_requirements_lock.py) -- never requirements.txt
    # itself, which pins only a handful of top-level packages and says
    # nothing about the transitive versions that actually determine
    # behaviour. Defaults to the sibling `<requirements_path stem>.lock`
    # (requirements.txt -> requirements.lock) so every real call site needs
    # no change; a caller may still pass an explicit path (tests using a
    # synthetic requirements file under tmp_path). Missing is a loud
    # ConfigError, never a silent fall-back to hashing requirements.txt
    # (CLAUDE.md NN14).
    lock_path = Path(dependency_lock_path) if dependency_lock_path is not None else requirements_path.with_suffix(".lock")
    if not lock_path.is_file():
        raise ConfigError(
            f"dependency_lock_hash requires a real lock file at {lock_path!s} -- generate one with "
            f"scripts/generate_requirements_lock.py (CLAUDE.md P2/P3 gate review item 8; "
            f"requirements.txt alone is not a lock)"
        )
    fields = {
        "source_table_versions": _canonical_json(source_table_versions),
        "uploaded_file_hashes": _canonical_json(uploaded_file_hashes),
        "reference_data_hashes": _canonical_json(_reference_data_hashes(reference_files)),
        "skill_content_hash": _skill_content_hash(skill_dir),
        "code_revision": _resolve_code_revision(code_revision, settings.code_revision),
        "dependency_lock_hash": sha256_bytes(lock_path.read_bytes()),
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
