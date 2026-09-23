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


# Keys under which a bare numeric literal is structurally required and is not a
# test threshold: version pins, minItems-shaped list values, thresholds.yaml's
# own `value` (the one place a real number belongs), and effective_date-shaped
# strings are not numeric so need no exemption.
_ALLOWED_NUMERIC_KEY_SUFFIXES = (".value", ".sample_days")


def test_plan_and_findings_yaml_have_no_numeric_literals_outside_thresholds():
    plan = yaml.safe_load((SKILLS_DIR / "tne_exco" / "plan.yaml").read_text())
    findings = yaml.safe_load((SKILLS_DIR / "tne_exco" / "findings.yaml").read_text())

    violations = []
    for label, doc in (("plan.yaml", plan), ("findings.yaml", findings)):
        found: list = []
        _walk_numeric_literals(doc, label, found)
        for path, value in found:
            if any(path.endswith(suffix) for suffix in _ALLOWED_NUMERIC_KEY_SUFFIXES):
                continue
            # A bare 0 as an `exclude`/filter comparison value (e.g. "amount > 0")
            # is a population-boundary constant, not a disguised threshold -- the
            # restricted evaluator itself allows exactly this literal for the
            # same reason (CLAUDE.md §4.6). Anything else is a violation.
            if value == 0:
                continue
            violations.append(f"{path} = {value!r}")

    assert not violations, "numeric literals outside thresholds.yaml:\n" + "\n".join(violations)
