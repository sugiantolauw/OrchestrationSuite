"""G11 (docs/specs/P6_narration_design.md §3.6, §11, §12 WP N2):

1. The 30-case golden set (`tests/fixtures/narration/g11_golden.yaml`, 10
   valid, 20 invalid) -- every case run through
   `orchestrator.narration.validate.validate_prose` and checked against its
   expected verdict and rule id(s). See that file's header for the two
   cases substituted for the design spec's candidate-level (C-2) examples,
   which belong to WP N8's not-yet-built `candidates.py`.
2. The template-parity check ("outside the 30", §3.6): each of SKILL-001's
   13 `findings.yaml` observation/recommendation templates, mechanically
   rewritten from `findings.yaml`'s bare `{name}` form into the typed
   `{class:name}` form a model must write, rendered through
   `orchestrator.narration.placeholders.render` and asserted byte-identical
   to `orchestrator.findings._format_template`'s output on the SAME run's
   real computed metrics -- proving the new renderer changes nothing about
   what an auditor sees. `findings.yaml` itself is never edited; the
   mechanical conversion happens only in this test file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from orchestrator.findings import _format_template, format_metric_value
from orchestrator.narration.placeholders import PlaceholderEntry, class_for_unit, render
from orchestrator.narration.validate import validate_prose

GOLDEN_PATH = Path(__file__).parent / "fixtures" / "narration" / "g11_golden.yaml"
SKILL_DIR = Path(__file__).parent.parent / "skills" / "tne_exco"
SYNTHETIC_DATA_DIR = Path(__file__).parent.parent / "synthetic_data"
AUDIT_PERIOD = ("2025-01-01", "2026-04-30")

# The 13 findings.yaml rules that are not T4.3 (SKILL-001 omits it -- see
# findings.yaml's own header comment -- so there is nothing to port here).
TEMPLATE_PARITY_RULE_IDS = [
    "T3_1a",
    "T3_1b",
    "T3_2a",
    "T3_3a",
    "T3_3b",
    "T4_1",
    "T4_2",
    "T4_4",
    "T5_1",
    "T5_2",
    "T6_1a",
    "T6_1c",
    "T6_1d",
]


def _entry(name: str, spec: dict) -> PlaceholderEntry:
    return PlaceholderEntry(name=name, unit=spec["unit"], value=spec["value"])


def _table(raw: dict | None) -> dict[str, PlaceholderEntry]:
    return {name: _entry(name, spec) for name, spec in (raw or {}).items()}


def _load_golden_cases() -> list[dict]:
    data = yaml.safe_load(GOLDEN_PATH.read_text())
    return data["cases"]


GOLDEN_CASES = _load_golden_cases()


# ---------------------------------------------------------------------------
# The 30-case golden set.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("case", GOLDEN_CASES, ids=[c["id"] for c in GOLDEN_CASES])
def test_g11_golden_case(case: dict) -> None:
    table = _table(case.get("table"))
    result = validate_prose(
        case["text"],
        table,
        field=case["field"],
        origin=case.get("origin", "model"),
        allowed_identifiers=case.get("allowed_identifiers", []),
        require_coverage=case.get("require_coverage", False),
        required_placeholders=case.get("required_placeholders"),
    )
    expected = case["expected"]
    assert result.valid is expected["valid"], (
        f"{case['id']}: expected valid={expected['valid']}, got violations "
        f"{[(v.rule_id, v.message) for v in result.violations]}"
    )
    assert result.rule_ids() == frozenset(expected.get("rule_ids", [])), (
        f"{case['id']}: expected rule ids {expected.get('rule_ids', [])}, "
        f"got {sorted(result.rule_ids())}"
    )
    if "rendered" in expected:
        assert render(case["text"], table) == expected["rendered"], f"{case['id']}: rendered text mismatch"


def test_golden_set_is_30_cases_10_valid_20_invalid() -> None:
    valid = [c for c in GOLDEN_CASES if c["expected"]["valid"]]
    invalid = [c for c in GOLDEN_CASES if not c["expected"]["valid"]]
    assert len(GOLDEN_CASES) == 30
    assert len(valid) == 10
    assert len(invalid) == 20


def test_every_invalid_case_names_its_expected_rule_id() -> None:
    for case in GOLDEN_CASES:
        if not case["expected"]["valid"]:
            assert case["expected"].get("rule_ids"), f"{case['id']}: invalid case with no expected rule id"


def test_every_valid_case_has_no_expected_rule_ids() -> None:
    for case in GOLDEN_CASES:
        if case["expected"]["valid"]:
            assert case["expected"].get("rule_ids", []) == [], f"{case['id']}: valid case names a rule id"


def test_case_ids_are_unique() -> None:
    ids = [c["id"] for c in GOLDEN_CASES]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Template parity: SKILL-001's findings.yaml, unchanged, rendered through the
# typed-placeholder renderer instead of `_format_template`.
# ---------------------------------------------------------------------------
_BARE_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _typed_conversion(template: str, class_by_name: dict[str, str]) -> str:
    """Mechanically rewrites a findings.yaml bare `{name}` template into the
    typed `{class:name}` form (§3.1) a model must write -- for this test
    only. `findings.yaml` is never edited; SKILL-001's real template text is
    read as-is and only the copy fed to `render()` is rewritten."""

    def _sub(m: "re.Match[str]") -> str:
        name = m.group(1)
        return f"{{{class_by_name[name]}:{name}}}"

    return _BARE_PLACEHOLDER_RE.sub(_sub, template)


pytestmark = pytest.mark.skipif(
    not SYNTHETIC_DATA_DIR.is_dir(), reason="synthetic_data/ not present in this checkout"
)


@pytest.fixture(scope="module")
def tne_skill():
    from orchestrator.skills import load_skill

    skill = load_skill(SKILL_DIR)
    skill.validate()
    return skill


@pytest.fixture(scope="module")
def tne_metrics(tne_skill):
    # The same real metrics an existing test (tests/test_skill_tne.py,
    # test_all_finding_templates_render_cleanly_against_real_metrics) uses
    # for every SKILL-001 finding: a real run of execute_skill over
    # synthetic_data/, not a hand-picked fixture -- this check exists
    # precisely to catch a unit/rendering mismatch a hand-picked table could
    # paper over.
    from orchestrator.contract import LocalFileDataSource
    from orchestrator.engine import execute_skill

    ds = LocalFileDataSource(root_dir=SYNTHETIC_DATA_DIR, sources=tne_skill.contract["sources"])
    result = execute_skill(
        tne_skill,
        data_source=ds,
        audit_period=AUDIT_PERIOD,
        run_context={"run_id": "test-g11-template-parity"},
    )
    return result.metrics


@pytest.mark.parametrize("rule_id", TEMPLATE_PARITY_RULE_IDS)
def test_template_parity_with_typed_placeholders(tne_skill, tne_metrics, rule_id: str) -> None:
    rule = next(r for r in tne_skill.findings["findings"] if r["id"] == rule_id)

    cited_names = rule.get("metrics_cited", [])
    missing = [n for n in cited_names if n not in tne_metrics]
    assert not missing, f"{rule_id}: metrics_cited references unknown metric(s) {missing}"
    metrics_cited = {n: tne_metrics[n] for n in cited_names}

    threshold_ids = rule.get("thresholds_cited", [])
    threshold_kwargs = {
        tid: format_metric_value(tne_skill.thresholds[tid]["value"], tne_skill.thresholds[tid]["unit"])
        for tid in threshold_ids
    }

    table: dict[str, PlaceholderEntry] = {
        n: PlaceholderEntry(n, m["unit"], m["value"]) for n, m in metrics_cited.items()
    }
    for tid in threshold_ids:
        spec = tne_skill.thresholds[tid]
        table[tid] = PlaceholderEntry(tid, spec["unit"], spec["value"])
    class_by_name = {n: class_for_unit(e.unit) for n, e in table.items()}

    for field in ("observation", "recommendation"):
        template_text = rule.get(field) or ""
        if not template_text.strip():
            continue
        expected = _format_template(template_text, metrics_cited, threshold_kwargs)
        typed_text = _typed_conversion(template_text, class_by_name)
        actual = render(typed_text, table)
        assert actual == expected, (
            f"{rule_id}.{field}: typed-placeholder render diverged from _format_template\n"
            f"  expected: {expected!r}\n  actual:   {actual!r}"
        )


def test_template_parity_covers_all_13_skill001_findings(tne_skill) -> None:
    # findings.yaml's own header: "T4.3 is omitted"; every other rule must
    # be exercised above, and no rule may be silently dropped from the list.
    rule_ids = [r["id"] for r in tne_skill.findings["findings"]]
    assert set(rule_ids) == set(TEMPLATE_PARITY_RULE_IDS)
    assert len(rule_ids) == 13
