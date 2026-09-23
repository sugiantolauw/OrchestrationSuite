"""Skill registration (CLAUDE.md §4.6, §4.8, §4.9): records a loaded Skill's
content as an immutable `skill_versions` row and seeds the T&E risk/control
register from the Skill's own `risk_control.yaml`. This is generic pipeline
code -- it reads whatever a Skill declares, never anything SKILL-001-specific.

Register entries are Skill-level, not engagement-scoped: `engagement_id` is
left unset (None) here. A risk or control only becomes engagement-scoped when
an engagement's own planning work adopts it -- that is a later phase's write,
not this one's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from orchestrator.skills import Skill


def _load_risk_control(skill_dir: Path) -> dict:
    path = skill_dir / "risk_control.yaml"
    if not path.is_file():
        return {"risks": [], "controls": []}
    return yaml.safe_load(path.read_text()) or {"risks": [], "controls": []}


def register_skill(skill: Skill, persistence, *, actor: str, now: str) -> dict[str, Any]:
    """Records `skill`'s content (manifest + contract + plan + findings +
    thresholds -- everything CLAUDE.md §3 NN7's skill_content_hash covers,
    matched here against the Skill's own `content_hash`) as a `skill_versions`
    row, then upserts every risk/control the Skill's `risk_control.yaml`
    declares. Idempotent: `record_skill_version` no-ops on a repeat
    skill_id+version with the same content hash (raises on a genuine content
    change under the same version -- CLAUDE.md §3 NN8, a Skill's content hash
    is what a run fingerprint pins to), and `upsert_risks`/`upsert_controls`
    are themselves idempotent upserts keyed on risk_id/control_id."""
    content = {
        "manifest": skill.manifest,
        "contract": skill.contract,
        "plan": skill.plan,
        "findings": skill.findings,
        "thresholds": skill.thresholds,
    }
    skill_version_row = persistence.record_skill_version(
        skill_id=skill.skill_id,
        version=skill.version,
        content_hash=skill.content_hash,
        content=content,
        created_by=actor,
        now=now,
    )

    rc = _load_risk_control(skill.skill_dir)

    risks = [
        {
            "risk_id": r["risk_id"],
            "title": r["title"],
            "description": r.get("description"),
            "category": r.get("category"),
            "status": "proposed",
            "source": "manual",
        }
        for r in rc.get("risks", [])
    ]
    controls = [
        {
            "control_id": c["control_id"],
            "risk_id": c["risk_id"],
            "title": c["title"],
            "description": c.get("description"),
            "type": c.get("type"),
            "frequency": c.get("frequency"),
        }
        for c in rc.get("controls", [])
    ]

    if risks:
        persistence.upsert_risks(risks, now=now)
    if controls:
        persistence.upsert_controls(controls, now=now)

    return {
        "skill_version": skill_version_row,
        "risks_registered": len(risks),
        "controls_registered": len(controls),
    }
