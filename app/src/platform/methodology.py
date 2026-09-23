"""Skill methodology metadata assembly.

For the T&E Skill (SKILL-001), every field here is read from the Skill
itself -- manifest.yaml (version/owner/status/description), catalogue.yaml
(rendered against thresholds.yaml, with provenance), and skill_versions
(the real publish history) -- via orchestrator.service.get_skill /
list_skill_versions (adapters). This module used to keep its own copy of
the test catalogue (app/src/test_catalogue.py, deleted) with a hardcoded
version, status and threshold text that drifted from the real Skill the
moment anyone edited skills/tne_exco/*.yaml without also editing this file
by hand (CLAUDE.md §3 NN16, P2/P3 gate review item 4). There is exactly one
source of truth now: the Skill's own files.

For other Skills, returns lightweight stub metadata clearly marked as
"under development" so the platform remains honest about what's implemented.
"""

from __future__ import annotations


def get_methodology(skill_id: str) -> dict:
    """Return methodology metadata for a given skill_id, read entirely from
    the connected backend (never a static registry in this module)."""
    if skill_id == "SKILL-001":
        return _tne_methodology(skill_id)
    return _stub_methodology(skill_id)


# ─── T&E Skill methodology (real, pulled from the Skill's own files) ────────

def _tne_methodology(skill_id: str) -> dict:
    from src.platform.adapters import get_skill, list_skill_versions

    skill = get_skill(skill_id) or {}
    tests = skill.get("tests") or []

    data_sources = []
    for spec in sorted(skill.get("sources") or [], key=lambda s: s.get("source", "")):
        data_sources.append({
            "key": spec.get("source"),
            "filename": spec.get("file"),
            "sheet": spec.get("sheet"),
            # Not declared by contract.yaml (only a runtime row count exists,
            # per run) -- left unset rather than fabricated (CLAUDE.md NN14);
            # the data_asset_card-style "expected_rows" fallback already
            # handles a missing value.
            "expected_rows": None,
        })

    # Category order: as first encountered in catalogue.yaml's own test
    # order (skill["tests"]) -- never a second, separately-maintained list
    # that can silently drift from the real file (CLAUDE.md NN16).
    categories: list[str] = []
    for t in tests:
        cat = t.get("category") or "Uncategorised"
        if cat not in categories:
            categories.append(cat)

    tests_by_category: dict[str, list[dict]] = {cat: [] for cat in categories}
    for t in tests:
        cat = t.get("category") or "Uncategorised"
        tests_by_category.setdefault(cat, []).append({
            "test_id": t.get("test_id"),
            "test_name": t.get("test_name"),
            "control_objective": t.get("control_objective"),
            "population": t.get("population"),
            "rule": t.get("rule"),
            # threshold text is already rendered against thresholds.yaml by
            # orchestrator.service.get_skill (never a raw "{...}" template);
            # threshold_provenance names which threshold id(s) it drew on so
            # the UI can label an analyst-set / pending-confirmation value
            # (CLAUDE.md §0.4/G8) without re-parsing the template itself.
            "threshold": t.get("threshold"),
            "threshold_provenance": t.get("threshold_provenance") or [],
        })

    source_names = [s["key"] for s in data_sources if s.get("key")]
    versions = list_skill_versions(skill_id)

    return {
        "skill_id": skill.get("skill_id", skill_id),
        "name": skill.get("name", "Unknown Skill"),
        "domain": skill.get("domain", ""),
        "version": skill.get("version", "?"),
        "owner": skill.get("owner", ""),
        "status": skill.get("status", "Draft"),
        "last_updated": skill.get("last_updated", ""),
        "purpose": skill.get("description") or "No description recorded in manifest.yaml.",
        "population": (
            (f"Declared contract sources: {', '.join(source_names)}." if source_names
             else "This Skill's contract declares no sources.")
            + " The audit period and population of interest are chosen per run, not fixed by the Skill."
        ),
        "data_sources": data_sources,
        "categories": categories,
        "tests_by_category": tests_by_category,
        "total_tests": len(tests),
        "risk_scoring": {
            "High": "Set by this test's own severity ladder (skills/<id>/findings.yaml) -- "
                    "see the Threshold column above for the specific value and its provenance.",
            "Medium": "Set by this test's own severity ladder -- see the Threshold column above.",
            "Low": "The ladder's fallback band when no severity threshold is exceeded.",
        },
        "outputs": [
            "Executive brief with headline exposure, risk mix, action summary",
            "Findings register with evidence-linked observations and recommendations",
            "Branded PowerPoint export (cover, exec summary, native charts, findings)",
            "Excel workbook with per-finding exception sheets and test catalogue",
            "Management action tracker with edit history and audit log",
        ],
        "evidence_traceability": (
            "Every finding cites specific metric keys. Every metric carries a source_ref "
            "identifying the source table version or uploaded-file hash, the source "
            "columns and the aggregation grain."
        ),
        "version_history": [
            {
                "version": v.get("version"),
                "date": (v.get("created_at") or "")[:10],
                "status": v.get("status"),
                "created_by": v.get("created_by"),
            }
            for v in versions
        ],
        "is_stub": False,
    }


# ─── Stub methodology for Skills still under development ────────────────────

_STUB_CONTENT = {
    "SKILL-002": {
        "purpose": (
            "Review accounts payable transactions for GST input tax credit eligibility. "
            "Validate vendor ABN registration, GST coding accuracy, and tax invoice "
            "compliance against ATO requirements."
        ),
        "planned_tests": [
            "GST claimed on vendors without valid ABN",
            "GST coding vs vendor registration status mismatch",
            "Tax invoice completeness against ATO requirements",
            "Reverse-charge / import GST treatment accuracy",
            "Concessional supply and input-tax adjustment tests",
        ],
        "planned_sources": [
            "AP transaction line items",
            "Vendor master with ABN and GST registration",
            "Tax invoice repository",
            "ATO ABN Lookup extract",
        ],
    },
    "SKILL-003": {
        "purpose": (
            "Assess emergency change requests for proper authorisation, retrospective "
            "approval, documentation completeness, and segregation-of-duties compliance."
        ),
        "planned_tests": [
            "Emergency changes without retrospective CAB approval",
            "Change requester and approver same person",
            "Missing rollback plan or test evidence",
            "Time-to-approve outside SLA",
        ],
        "planned_sources": [
            "ServiceNow change management extract",
            "CAB minutes and approval records",
            "Identity and access records for SoD checks",
        ],
    },
    "SKILL-004": {
        "purpose": (
            "Validate user access privileges across critical systems. Identify "
            "orphaned accounts, excessive privileges, segregation-of-duties conflicts, "
            "and dormant accounts."
        ),
        "planned_tests": [
            "Terminated employees with active accounts",
            "Privileged access without approval evidence",
            "SoD conflicts across finance systems",
            "Dormant privileged accounts (> 90 days inactive)",
        ],
        "planned_sources": [
            "HR terminations register",
            "System access entitlements per critical app",
            "Privileged access management (PAM) logs",
        ],
    },
    "SKILL-005": {
        "purpose": (
            "Compare deployed software instances against purchased licence entitlements. "
            "Identify over-deployment, under-utilisation, and non-compliant installations."
        ),
        "planned_tests": [
            "Deployed installations vs entitlements",
            "Utilisation rate per licence type",
            "Non-standard software on managed devices",
        ],
        "planned_sources": [
            "Endpoint management inventory",
            "Software licence purchase register",
            "Vendor audit reports",
        ],
    },
}


def _stub_methodology(skill_id: str) -> dict:
    from src.platform.adapters import get_skill

    skill = get_skill(skill_id) or {}
    stub = _STUB_CONTENT.get(skill_id, {})
    return {
        "skill_id": skill_id,
        "name": skill.get("name", "Unknown Skill"),
        "domain": skill.get("domain", ""),
        "version": skill.get("version", "0.1"),
        "owner": skill.get("owner", ""),
        "status": skill.get("status", "Draft"),
        "last_updated": skill.get("last_updated", ""),
        "purpose": stub.get("purpose", skill.get("description", "")),
        "planned_tests": stub.get("planned_tests", []),
        "planned_sources": stub.get("planned_sources", []),
        "is_stub": True,
    }
