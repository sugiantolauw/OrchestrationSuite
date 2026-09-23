from __future__ import annotations

import re
from pathlib import Path

import yaml

ORCHESTRATOR_DIR = Path(__file__).parent.parent / "orchestrator"
SKILLS_DIR = Path(__file__).parent.parent / "skills"

_PLACEHOLDER_PATTERNS = [
    re.compile(r"\b132\b"),  # computation.py compute_test_4_3_cached's hardcoded `flagged`
    re.compile(r"\b152[_,]?921\b"),  # computation.py's hardcoded total_records
    re.compile(r"intl_exceptions"),
    re.compile(r"[Bb]ased on audit findings"),
    re.compile(r"[Ff]rom audit findings"),
    re.compile(r"\baus_rate\s*=\s*500\b"),
    re.compile(r"\$1,?000\b"),
    re.compile(r"\bdaily_limit\s*=\s*1000\b"),
]


def _all_source_files():
    for base in (ORCHESTRATOR_DIR, SKILLS_DIR):
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path
        for path in base.rglob("*.yaml"):
            yield path
        for path in base.rglob("*.yml"):
            yield path


def test_no_known_placeholder_result_values_anywhere():
    violations = []
    for path in _all_source_files():
        text = path.read_text(errors="replace")
        for pattern in _PLACEHOLDER_PATTERNS:
            if pattern.search(text):
                violations.append(f"{path}: matched {pattern.pattern!r}")
    assert not violations, "known placeholder/hardcoded-result patterns found:\n" + "\n".join(violations)


def _walk_numeric_literals(obj, path, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            _walk_numeric_literals(v, f"{path}.{k}", out)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _walk_numeric_literals(item, f"{path}[{i}]", out)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.append((path, obj))


# N3: no blanket "any key ending in .value is exempt" rule -- that would
# silently wave through a future non-zero literal planted at ANY `.value` key
# (e.g. a disguised threshold slipped into a filter). Every exempted path is
# named explicitly, each with its own reason a bare number belongs there and
# is not a disguised, unreviewed threshold. A bare 0 as a population
# sign/existence filter (e.g. "amount > 0", never `gt 0.0` or any other value)
# is a structural boundary, not a policy number -- the restricted evaluator
# itself allows exactly this literal for the same reason (CLAUDE.md §4.6);
# every entry here is exactly that shape and no other.
_ALLOWED_NUMERIC_LITERAL_PATHS: dict[str, str] = {
    "plan.yaml.populations.p_exp_split.filters[2].value": (
        "T5.1 spec: p_exp_split excludes non-positive amounts before split detection "
        "(a credit or zero-amount line cannot be part of a split claim) -- sign filter, not a threshold."
    ),
    "plan.yaml.populations.p_exp_hv.filters[1].value": (
        "T4.4 spec 'Population: P_EXP, amount > 0' -- sign filter excluding credits from the "
        "high-value population, not the $5,000 threshold itself (that is thresholds.high_value_limit)."
    ),
    "plan.yaml.populations.p_exp_dup.filters[1].value": (
        "T5.2 spec: p_exp_dup excludes non-positive amounts before duplicate detection -- sign filter."
    ),
    "plan.yaml.populations.t61d_pop.filters[3].value": (
        "N5: t61d_pop excludes non-positive amounts so a credit is never netted into a daily "
        "total -- sign filter, not the per-diem limit (thresholds/per_diem_rates drive that)."
    ),
    "plan.yaml.populations.t61d_pop_dom.filters[3].value": (
        "N5: same sign filter as t61d_pop, applied to the domestic-split population."
    ),
    "plan.yaml.populations.t61d_pop_int.filters[3].value": (
        "N5: same sign filter as t61d_pop, applied to the international-split population."
    ),
}


def test_plan_and_findings_yaml_have_no_numeric_literals_outside_thresholds():
    plan = yaml.safe_load((SKILLS_DIR / "tne_exco" / "plan.yaml").read_text())
    findings = yaml.safe_load((SKILLS_DIR / "tne_exco" / "findings.yaml").read_text())

    violations = []
    for label, doc, prefix in (("plan.yaml", plan, "plan.yaml"), ("findings.yaml", findings, "findings.yaml")):
        found: list = []
        _walk_numeric_literals(doc, prefix, found)
        for path, value in found:
            if path in _ALLOWED_NUMERIC_LITERAL_PATHS:
                assert value == 0, f"{path}: allow-listed sign filter must be exactly 0, got {value!r}"
                continue
            violations.append(f"{path} = {value!r}")

    assert not violations, "numeric literals outside thresholds.yaml:\n" + "\n".join(violations)


def test_allow_listed_numeric_literal_paths_still_exist():
    # Guards the allow-list itself against drift: every entry must correspond
    # to a real path in the current plan.yaml, or it is dead and should be
    # removed (and, if a path was renamed, this catches the stale entry
    # silently exempting the WRONG key going forward).
    plan = yaml.safe_load((SKILLS_DIR / "tne_exco" / "plan.yaml").read_text())
    findings = yaml.safe_load((SKILLS_DIR / "tne_exco" / "findings.yaml").read_text())
    found: list = []
    _walk_numeric_literals(plan, "plan.yaml", found)
    _walk_numeric_literals(findings, "findings.yaml", found)
    live_paths = {path for path, _ in found}
    stale = set(_ALLOWED_NUMERIC_LITERAL_PATHS) - live_paths
    assert not stale, f"allow-list entries no longer present in plan.yaml/findings.yaml: {stale}"
