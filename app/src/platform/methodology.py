"""Skill methodology metadata assembly.

For the T&E Skill (SKILL-001), pulls real methodology content from the
existing test catalogue plus the Skill's own contract (via
orchestrator.service.get_skill / adapters), so the viewer reflects what the
connected app actually runs — not a copy of the prototype's file registry
(reference_app/src/data_loader.py, deliberately not ported: CLAUDE.md §0.2
names it among the modules that must not survive into the build).

For other Skills, returns lightweight stub metadata clearly marked as
"under development" so the platform remains honest about what's implemented.
"""

from __future__ import annotations


def get_methodology(skill_id: str, sources: list | None = None) -> dict:
    """Return methodology metadata for a given skill_id. `sources` is
    orchestrator.service.get_skill(skill_id)["sources"] — a list of
    {"source": name, "columns": [str, ...]} — passed in by the caller
    rather than this module reaching into a static registry."""
    if skill_id == "SKILL-001":
        return _tne_methodology(sources)
    return _stub_methodology(skill_id)


# ─── T&E Skill methodology (real, pulled from live catalogue + contract) ────

def _tne_methodology(sources: list | None = None) -> dict:
    from src.test_catalogue import TEST_CATALOGUE, CATEGORY_ORDER

    data_sources = []
    for spec in sorted((sources or []), key=lambda s: s.get("source", "")):
        data_sources.append({
            "key": spec.get("source"),
            "columns_declared": len(spec.get("columns") or []),
        })

    # Group tests by category
    tests_by_category = {cat: [] for cat in CATEGORY_ORDER}
    for t in TEST_CATALOGUE:
        cat = t.get("category", "Uncategorised")
        tests_by_category.setdefault(cat, []).append({
            "test_id": t["test_id"],
            "test_name": t["test_name"],
            "control_objective": t["control_objective"],
            "population": t["population"],
            "rule": t["rule"],
            "threshold": t["threshold"],
            "source_file_key": t.get("source_file_key"),
        })

    return {
        "skill_id": "SKILL-001",
        "name": "ExCo T&E Executive Diligence",
        "domain": "Travel & Entertainment",
        "version": "1.2",
        "owner": "Internal Audit",
        "status": "Published",
        "last_updated": "2026-07-15",
        "purpose": (
            "Assess executive T&E spend against Optus policy for the audit period. "
            "The Skill runs 14 deterministic tests across pre-travel controls, spend "
            "compliance, fraud risk indicators, and approval effectiveness — every "
            "metric is traceable back to a specific source file."
        ),
        "population": (
            "All expense claims, travel requests, approvals, and attendee records "
            "for members of the Optus Executive Committee (ExCo) within the audit "
            "period (01 Jan 2025 – 30 Apr 2026)."
        ),
        "data_sources": data_sources,
        "categories": CATEGORY_ORDER,
        "tests_by_category": tests_by_category,
        "total_tests": len(TEST_CATALOGUE),
        "risk_scoring": {
            "High": "Material exposure OR systemic control failure OR fraud indicator",
            "Medium": "Non-trivial exposure OR recurring control weakness",
            "Low": "Isolated exception OR minor policy deviation",
        },
        "outputs": [
            "Executive brief with headline exposure, risk mix, action summary",
            "Findings register with evidence-linked observations and recommendations",
            "Optus-branded PowerPoint export (cover, exec summary, native charts, findings)",
            "Excel workbook with per-finding exception sheets and test catalogue",
            "Management action tracker with edit history and audit log",
        ],
        "evidence_traceability": (
            "Every finding cites specific metric keys. Every metric carries a "
            "source_file identifier so the reader can trace the number to the "
            "originating file. The evidence drawer surfaces this trail per finding."
        ),
        "version_history": [
            {"version": "1.2", "date": "2026-07-15", "change": "Added T3.3a (late booking) and T6.1d (daily spend limit) tests"},
            {"version": "1.1", "date": "2026-05-02", "change": "Introduced deterministic risk scoring; separated per-approver detail"},
            {"version": "1.0", "date": "2026-03-14", "change": "Initial published methodology — 12 tests across 4 categories"},
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
