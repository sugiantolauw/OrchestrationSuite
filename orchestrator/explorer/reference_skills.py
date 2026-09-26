"""Builds the `reference_skills_json` planner-payload block (CLAUDE.md §4.5;
docs/specs/P6_P8_explorer_llm_design.md §4.4): 1-2 already-loaded reference
Skills, digested down to the parts the planner needs to see HOW a test,
its finding rules, and their thresholds are written -- never the Skill's
actual data or reference tables.

WHICH Skills to use as references (`EXPLORER_REFERENCE_SKILL_IDS`,
reading `skills/`) is a run-setup concern for a later work package
(§4.2 `start_explorer_run`); this module only knows how to turn an
already-loaded `orchestrator.skills.Skill` into its digest."""

from __future__ import annotations

import json
from typing import Any

from orchestrator.expr import compile_expr
from orchestrator.skills import Skill

_MAX_DIGEST_CHARS = 30_000


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _walk_collect_strings(obj: Any, key: str, out: set[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, str):
                out.add(v)
            _walk_collect_strings(v, key, out)
    elif isinstance(obj, list):
        for item in obj:
            _walk_collect_strings(item, key, out)


def _first_test_per_primitive(plan_tests: list[dict]) -> list[dict]:
    seen_primitives: set[str] = set()
    out: list[dict] = []
    for t in plan_tests:
        primitive = t.get("primitive")
        if primitive is None or primitive in seen_primitives:
            continue
        seen_primitives.add(primitive)
        out.append(t)
    return out


def _test_digest(skill: Skill, test: dict) -> dict:
    """BUG-EXPLORER-PLAN-2 (independent review round 5, RUN-99373993B1E0):
    `findings` used to sit NESTED inside this returned dict, one worked
    example per test -- but PLAN_PROPOSAL_SCHEMA (wire_schema.py) puts
    `findings` in a FLAT top-level array with a `test_key` back-reference
    (§4.5). The planner prompt (planner_system.md rule 16) tells the model
    to copy REFERENCE SKILLS' format exactly; shown a nested example, GPT-
    OSS dutifully reproduced the nesting in its own proposal on every one
    of the run's five tests, which then failed wire-schema validation
    top-to-bottom ("Additional properties are not allowed") and left zero
    valid tests. `findings` is now built separately, at the digest level,
    in the schema's own flat/`test_key` shape (build_reference_skill_
    digest) -- this function returns only the test's own fields."""
    test_id = test["test_id"]

    findings = [
        f for f in skill.findings.get("findings", []) if f.get("test_id") == test_id
    ]

    threshold_ids: set[str] = set()
    _walk_collect_strings(test.get("params", {}), "threshold", threshold_ids)
    for f in findings:
        threshold_ids |= set(f.get("thresholds_cited", []))
        # `thresholds.<id>` attribute references inside trigger/severity[].when
        # (CLAUDE.md §4.6, orchestrator.expr) -- a threshold a rule ENFORCES
        # but never quotes in prose is still part of "how this test is
        # written", so the planner sees its value/unit/provenance too.
        try:
            threshold_ids |= compile_expr(f["trigger"]).threshold_ids
        except Exception:  # noqa: BLE001 -- a reference Skill is already load_skill-validated
            pass
        for rule in f.get("severity", []):
            if "when" in rule:
                try:
                    threshold_ids |= compile_expr(rule["when"]).threshold_ids
                except Exception:  # noqa: BLE001
                    pass
    thresholds = {
        tid: {
            "value": skill.thresholds[tid]["value"],
            "unit": skill.thresholds[tid]["unit"],
            "description": skill.thresholds[tid].get("description"),
            "provenance": skill.thresholds[tid].get("provenance"),
        }
        for tid in sorted(threshold_ids) if tid in skill.thresholds
    }

    pop_keys: set[str] = set()
    for param_key in ("population", "left_population", "right_population"):
        v = test.get("params", {}).get(param_key)
        if v:
            pop_keys.add(v)
    populations = {
        key: skill.plan.get("populations", {}).get(key, {}) for key in sorted(pop_keys)
    }

    columns: list[dict] = []
    seen_columns: set[tuple[str, str]] = set()
    for pop_key, pop_cfg in populations.items():
        source = pop_cfg.get("source")
        src_cfg = skill.contract.get("sources", {}).get(source, {})
        for col_name, col_cfg in (src_cfg.get("columns") or {}).items():
            key = (source, col_name)
            if key in seen_columns:
                continue
            seen_columns.add(key)
            columns.append({
                "source": source, "name": col_name,
                "type": col_cfg.get("type"), "pii": bool(col_cfg.get("pii")),
            })

    return {
        "test_id": test_id,
        "primitive": test.get("primitive"),
        "control_objective": test.get("control_objective"),
        "params": test.get("params", {}),
        "thresholds": thresholds,
        "contract_columns": columns,
        "populations": populations,
    }


def _finding_digest(test_id: str, f: dict) -> dict:
    """One reference finding in the wire schema's OWN flat shape (`key`,
    `test_key`) -- see `_test_digest`'s docstring."""
    return {
        "key": f["id"],
        "test_key": test_id,
        "trigger": f["trigger"],
        "severity": f["severity"],
        "metrics_cited": f.get("metrics_cited", []),
        "thresholds_cited": f.get("thresholds_cited", []),
        "monetary_basis": f.get("monetary_basis"),
        "observation": f.get("observation"),
        "recommendation": f.get("recommendation"),
        "management_questions": f.get("management_questions", []),
    }


def build_reference_skill_digest(skill: Skill) -> dict:
    """One reference Skill's digest (§4.4): id/version/status/content_hash,
    plus the first test in plan order for each distinct primitive the
    Skill uses, and (BUG-EXPLORER-PLAN-2) a FLAT `findings` array in the
    wire schema's own `test_key`-linked shape -- never nested inside a
    test, so a model copying this example's FORMAT (planner_system.md rule
    16) produces a proposal that actually validates. Capped at 30,000
    characters by dropping trailing tests, each with its own findings
    (deterministic: proposal/plan order, never re-sorted by size)."""
    plan_tests = [t for t in skill.plan.get("tests", []) if "not_testable" not in t]
    chosen = _first_test_per_primitive(plan_tests)
    pairs = [
        (
            _test_digest(skill, t),
            [
                _finding_digest(t["test_id"], f)
                for f in skill.findings.get("findings", [])
                if f.get("test_id") == t["test_id"]
            ],
        )
        for t in chosen
    ]

    digest = {
        "id": skill.manifest.get("id"),
        "version": skill.manifest.get("version"),
        "status": skill.manifest.get("status"),
        "content_hash": skill.content_hash,
        "tests": [pair[0] for pair in pairs],
        "findings": [f for pair in pairs for f in pair[1]],
    }
    while len(_canonical_json(digest)) > _MAX_DIGEST_CHARS and pairs:
        pairs = pairs[:-1]
        digest["tests"] = [pair[0] for pair in pairs]
        digest["findings"] = [f for pair in pairs for f in pair[1]]
    return digest


def build_reference_skills_digests(skills: list[Skill]) -> dict[str, dict]:
    return {skill.skill_id: build_reference_skill_digest(skill) for skill in skills}
