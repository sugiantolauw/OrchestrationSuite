"""BUG-SKILLVER-1 guard (independent review round 3, 2026-09-25): a repo
Skill's content must never change without its manifest.yaml version
bumping -- the shared `skill_versions` ledger refuses a content change
under an unchanged version (`orchestrator.skill_registry.register_skill`,
CLAUDE.md §9C "Skill versioning and rollback"), so a missed bump
(`skills/tne_exco/findings.yaml` changed 154 lines in commit 7d46554
without one) breaks every fresh run of that Skill, permanently, on a
shared workspace -- and there is no in-product supersede path for an
already-recorded `(skill_id, version)` row.

Scoped to real repo Skills under `skills/<id>/`, each pinned by its own
`content.lock` (`{version, content_hash}`) -- never Explorer draft Skills
(materialised to a temp directory, `orchestrator/explorer/materialise.py`,
no `content.lock` there) and never the fixture Skills under
`tests/fixtures/skills/` (outside `skills/`, no `content.lock` either).
This loop only ever visits a directory that HAS one, so nothing needs an
explicit exclude list -- adding a `content.lock` to a new Skill is what
opts it into this guard."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from orchestrator.skills import load_skill

SKILLS_ROOT = Path(__file__).parent.parent / "skills"


def _skill_dirs_with_lock() -> list[Path]:
    if not SKILLS_ROOT.is_dir():
        return []
    return sorted(d for d in SKILLS_ROOT.iterdir() if d.is_dir() and (d / "content.lock").is_file())


@pytest.mark.parametrize("skill_dir", _skill_dirs_with_lock(), ids=lambda p: p.name)
def test_skill_content_matches_its_pinned_lock(skill_dir: Path):
    lock = yaml.safe_load((skill_dir / "content.lock").read_text())
    skill = load_skill(skill_dir)

    assert skill.version == lock["version"], (
        f"{skill_dir.name}: content.lock pins version {lock['version']!r} but manifest.yaml "
        f"now says {skill.version!r} -- regenerate content.lock for the new version"
    )
    assert skill.content_hash == lock["content_hash"], (
        f"{skill_dir.name}: this Skill's content changed under version {skill.version!r} "
        f"without a version bump (content_hash is now {skill.content_hash!r}, content.lock "
        f"still pins {lock['content_hash']!r}) -- bump the version and update the lock"
    )


def test_at_least_one_repo_skill_is_covered_by_this_guard():
    assert _skill_dirs_with_lock(), "no skills/<id>/content.lock found -- this guard is checking nothing"
