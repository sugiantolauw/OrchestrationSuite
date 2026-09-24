from __future__ import annotations

import importlib.util
import json
import string
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import jsonschema
import pandas as pd
import yaml

from orchestrator.expr import ExpressionError, compile_expr
from orchestrator.explorer.iso4217 import ISO4217_CODES
from orchestrator.fingerprint import skill_content_hash
from orchestrator.primitives import PRIMITIVES

_SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"

_REQUIRED_FILES = (
    "manifest.yaml",
    "contract.yaml",
    "plan.yaml",
    "findings.yaml",
    "thresholds.yaml",
    # N10: a Skill's risk/control register is not optional -- plan.yaml's
    # control_id/risk_id fields (CLAUDE.md §4.9) reference it, and a Skill
    # published without one has no risk-and-control matrix behind its tests.
    "risk_control.yaml",
)


class SkillValidationError(Exception):
    def __init__(self, violations: list[str]):
        self.violations = list(violations)
        super().__init__("; ".join(self.violations))


def _load_schema(name: str) -> dict:
    return json.loads((_SCHEMAS_DIR / f"{name}.schema.json").read_text())


_SCHEMAS: dict[str, dict] = {
    "manifest": _load_schema("manifest"),
    "contract": _load_schema("contract"),
    "plan": _load_schema("plan"),
    "findings": _load_schema("findings"),
    "thresholds": _load_schema("thresholds"),
}


def _load_yaml_validated(path: Path, schema: dict) -> dict:
    if not path.is_file():
        raise SkillValidationError([f"missing required Skill file: {path.name}"])
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise SkillValidationError([f"{path.name}: could not parse YAML: {exc}"]) from exc
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as exc:
        raise SkillValidationError([f"{path.name}: schema violation at {list(exc.absolute_path)}: {exc.message}"]) from exc
    return data


def _load_references(reference_dir: Path) -> dict[str, Any]:
    references: dict[str, Any] = {}
    if not reference_dir.is_dir():
        return references
    for p in sorted(reference_dir.iterdir()):
        if not p.is_file():
            continue
        if p.suffix in (".yaml", ".yml"):
            references[p.stem] = yaml.safe_load(p.read_text())
        elif p.suffix == ".csv":
            references[p.stem] = pd.read_csv(p)
    return references


def _load_prompts(prompts_dir: Path) -> dict[str, str]:
    prompts: dict[str, str] = {}
    if not prompts_dir.is_dir():
        return prompts
    for p in sorted(prompts_dir.rglob("*")):
        if p.is_file():
            prompts[str(p.relative_to(prompts_dir))] = p.read_text()
    return prompts


def _load_module(path: Path, module_name: str):
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@dataclass
class Skill:
    skill_dir: Path
    manifest: dict
    contract: dict
    plan: dict
    findings: dict
    thresholds: dict
    references: dict[str, Any] = field(default_factory=dict)
    prompts: dict[str, str] = field(default_factory=dict)
    custom_primitives: dict[str, Any] = field(default_factory=dict)
    custom_derivations: dict[str, Callable] = field(default_factory=dict)
    workspace_fn: Callable | None = None
    content_hash: str | None = None

    @property
    def skill_id(self) -> str:
        return self.manifest["id"]

    @property
    def version(self) -> str:
        return self.manifest["version"]

    def load(self) -> "Skill":
        return self

    def custom_tests(self) -> dict[str, Any]:
        return self.custom_primitives

    def render_workspace(self, run_id: str):
        if self.workspace_fn is None:
            raise SkillValidationError([f"skill {self.skill_id!r} has no workspace.py"])
        return self.workspace_fn(run_id)

    def validate(self) -> None:
        validate_skill(self)


def load_skill(skill_dir: str | Path) -> Skill:
    skill_dir = Path(skill_dir)
    for name in _REQUIRED_FILES:
        if not (skill_dir / name).is_file():
            raise SkillValidationError([f"missing required Skill file: {name}"])

    manifest = _load_yaml_validated(skill_dir / "manifest.yaml", _SCHEMAS["manifest"])
    contract = _load_yaml_validated(skill_dir / "contract.yaml", _SCHEMAS["contract"])
    plan = _load_yaml_validated(skill_dir / "plan.yaml", _SCHEMAS["plan"])
    findings = _load_yaml_validated(skill_dir / "findings.yaml", _SCHEMAS["findings"])
    thresholds = _load_yaml_validated(skill_dir / "thresholds.yaml", _SCHEMAS["thresholds"])

    references = _load_references(skill_dir / "reference")
    prompts = _load_prompts(skill_dir / "prompts")

    custom_module = _load_module(skill_dir / "custom.py", f"skill_custom_{manifest.get('id', 'unknown')}")
    custom_primitives = getattr(custom_module, "CUSTOM_PRIMITIVES", {}) if custom_module else {}
    custom_derivations = getattr(custom_module, "CUSTOM_DERIVATIONS", {}) if custom_module else {}

    workspace_module = _load_module(skill_dir / "workspace.py", f"skill_workspace_{manifest.get('id', 'unknown')}")
    workspace_fn = getattr(workspace_module, "render_workspace", None) if workspace_module else None

    return Skill(
        skill_dir=skill_dir,
        manifest=manifest,
        contract=contract,
        plan=plan,
        findings=findings,
        thresholds=thresholds,
        references=references,
        prompts=prompts,
        custom_primitives=custom_primitives,
        custom_derivations=custom_derivations,
        workspace_fn=workspace_fn,
        content_hash=skill_content_hash(skill_dir),
    )


# The Explorer materialiser (orchestrator.explorer.materialise) writes
# exactly these seven files -- the six required Skill files plus
# catalogue.yaml (CLAUDE.md §4.11's "Files" table). Nothing else may come
# out of a ledger row: `custom.py`/`workspace.py` are explicitly never
# written by Explorer (§4.4/§4.5 "Explorer does not generate code"), so
# `load_skill_from_ledger` refuses them by name, not merely by omission --
# defence in depth against a `skill_versions` row a compromised/mistaken
# writer path could otherwise smuggle code through.
_LEDGER_ALLOWED_FILES = _REQUIRED_FILES + ("catalogue.yaml",)


def load_skill_from_ledger(row: dict) -> Skill:
    """Loads a Skill from a `skill_versions` row (CLAUDE.md §4.11;
    docs/specs/P6_P8_explorer_llm_design.md §4.11 "Ledger content and
    loading"): an Explorer-confirmed run's materialised Skill, or a saved
    Explorer draft, neither of which lives in `skills/` on disk.

    1. Rejects any file entry ending in `.py`, or outside
       `_LEDGER_ALLOWED_FILES` -- code never executes from the database.
    2. Writes the files to a process-local cache directory keyed by
       `content_hash` (idempotent: a repeat call for the same hash reuses
       the directory rather than re-writing it).
    3. Calls `load_skill(dir)`.
    4. Asserts `skill.content_hash == row["content_hash"]`.
    """
    skill_id = row.get("skill_id", "?")
    content_hash = row["content_hash"]
    files: dict[str, str] = row["content"]["files"]

    violations = [
        f"ledger Skill {skill_id!r} contains a disallowed file: {path!r} "
        f"(only {sorted(_LEDGER_ALLOWED_FILES)} may be loaded from the ledger)"
        for path in files
        if path.endswith(".py") or path not in _LEDGER_ALLOWED_FILES
    ]
    if violations:
        raise SkillValidationError(violations)

    cache_root = Path(tempfile.gettempdir()) / "orchestrator_skill_ledger_cache" / content_hash
    if not cache_root.is_dir():
        cache_root.mkdir(parents=True, exist_ok=True)
        for path, text in files.items():
            (cache_root / path).write_text(text)

    skill = load_skill(cache_root)
    if skill.content_hash != content_hash:
        raise SkillValidationError([
            f"ledger Skill {skill_id!r} content_hash mismatch: stored {content_hash!r}, "
            f"re-hashed to {skill.content_hash!r} -- the cache directory may have been tampered with"
        ])
    return skill


def plan_test_flags(plan_tests: list[dict]) -> list[dict]:
    """Flattens plan.yaml's `tests` into {test_id, flag, primitive} rows --
    one row per RF_* flag a plan test can actually write to flagged_rows.

    A plan test's top-level `flag` is not always the complete set: some
    primitives write MORE than one flag from params keyed `flag_*`
    (split_detection's `flag_same_day`/`flag_window`, duplicate_detection's
    `flag`/`flag_oop_card`) -- e.g. T5.1 also writes RF_CS_SplitClaims_Window,
    which a caller that only read the top-level `flag` field would silently
    miss (CLAUDE.md P2/P3 gate review item 5: this is what under-counted
    T5.1's exposure and the flag_to_test drill-down alike). Collect every
    `params` key equal to "flag" or starting with "flag_", not just the one
    at the top level, so this is correct for any current or future primitive
    without per-primitive special-casing here. A not_testable test's flags
    are its own declared (plural) list, with primitive=None since none ran.

    Shared by orchestrator.service.get_skill (plan_tests/flag_to_test, for
    the UI drill-down) and orchestrator.nodes.fieldwork.prioritise (exposure
    de-duplication) so both draw on exactly the same flag set."""
    entries: list[dict] = []
    for t in plan_tests:
        test_id = t["test_id"]
        if "not_testable" in t:
            for flag in t["not_testable"].get("flags", []):
                entries.append({"test_id": test_id, "flag": flag, "primitive": None})
            continue
        flags: list[str] = []
        seen: set[str] = set()
        top_level = t.get("flag")
        if top_level:
            flags.append(top_level)
            seen.add(top_level)
        for key, value in (t.get("params") or {}).items():
            if not isinstance(value, str) or not value:
                continue
            if key == "flag" or key.startswith("flag_"):
                if value not in seen:
                    flags.append(value)
                    seen.add(value)
        for flag in flags:
            entries.append({"test_id": test_id, "flag": flag, "primitive": t.get("primitive")})
    return entries


# Metric `kind`s whose value is an additive dollar figure (a running total the
# primitive built up), as opposed to a ceiling/count/rate. Only these, paired
# with a real currency unit, name a genuine "amount at risk" -- e.g.
# `daily_over_max_*` is `kind: max` with a currency unit too (T6.1d's worst
# single day), and summing it into exposure alongside `daily_over_amount_*`
# would double-count.
_ADDITIVE_AMOUNT_METRIC_KINDS = {"sum", "value", "excess", "sum_where"}


def plan_test_amount_metrics(plan_tests: list[dict]) -> dict[str, set[str]]:
    """Flattens plan.yaml's `tests` into {test_id: {amount metric names}} --
    every metric a test's primitive declares with an additive dollar `kind`
    AND a real currency unit -- any ISO-4217 code, not only "AUD" (docs/specs/
    P6_P8_explorer_llm_design.md §4.12 item 3: an Explorer-materialised
    Skill's evidenced currency may be any code; a Playbook Skill's plan.yaml
    still always says "AUD", so this is a pure generalisation, not a
    behaviour change for SKILL-001). CLAUDE.md P2/P3 gate review item 1: a
    finding's `exposure_amount` must be the sum of its OWN cited amount
    metric(s), the same number its observation
    text renders -- never a value re-derived from raw rows by a SEPARATE piece
    of exposure-de-duplication logic that can (and did, for T5.2) disagree
    with what the primitive itself computed. duplicate_detection's
    `duplicate_amount` is "sum of extra lines beyond the first in each
    group" (spec T5.2) -- a shape no generic row/group summation can
    reconstruct without reproducing that exact rule, whereas reading the
    metric the primitive already computed is correct by construction for
    every primitive, present or future, with no per-primitive special case
    here.

    Shared by orchestrator.nodes.fieldwork.prioritise, so a finding's
    exposure is always drawn from the identical metric set its findings.yaml
    rule cites via `metrics_cited` (validate_skill enforces every
    metrics_cited name is one of these test's own declared metrics, so this
    is a subset of -- and only ever narrower than -- what the finding
    actually renders in prose)."""
    out: dict[str, set[str]] = {}
    for t in plan_tests:
        test_id = t["test_id"]
        metrics_spec = (t.get("params") or {}).get("metrics") or {}
        names = {
            name
            for name, spec in metrics_spec.items()
            if spec.get("unit") in ISO4217_CODES and spec.get("kind") in _ADDITIVE_AMOUNT_METRIC_KINDS
        }
        if names:
            out[test_id] = names
    return out


def _walk_collect(obj: Any, key: str, out: set[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, str):
                out.add(v)
            _walk_collect(v, key, out)
    elif isinstance(obj, list):
        for item in obj:
            _walk_collect(item, key, out)


def _collect_all(container: Any, key: str) -> set[str]:
    out: set[str] = set()
    _walk_collect(container, key, out)
    return out


_TEMPLATE_FORMATTER = string.Formatter()


def _template_placeholders(template: str) -> set[str]:
    return {
        field_name
        for _, field_name, _, _ in _TEMPLATE_FORMATTER.parse(template)
        if field_name
    }


def validate_skill(skill: Skill) -> None:
    violations: list[str] = []

    populations = skill.plan.get("populations", {})
    tests = skill.plan.get("tests", [])
    thresholds = skill.thresholds
    reference_names = set(skill.references) | {"audit_period"}

    known_metric_names: set[str] = set()
    seen_test_ids: set[str] = set()

    # B2 (CLAUDE.md P2/P3 gate review): a source's declared entry_key
    # (contract.yaml) is what the run headline (orchestrator.nodes.fieldwork.
    # prioritise) de-duplicates a monetary finding's flagged rows on -- every
    # column it names must be a real column of that same source.
    for src_name, src_cfg in skill.contract.get("sources", {}).items():
        entry_key = src_cfg.get("entry_key")
        if not entry_key:
            continue
        contract_columns = src_cfg.get("columns", {})
        unknown_entry_key_cols = [c for c in entry_key if c not in contract_columns]
        if unknown_entry_key_cols:
            violations.append(
                f"contract.sources.{src_name}.entry_key {unknown_entry_key_cols} not a column of {src_name!r}"
            )

    for source_name, pop_cfg in populations.items():
        src = pop_cfg.get("source")
        if src not in skill.contract.get("sources", {}):
            violations.append(f"populations.{source_name}: unknown source {src!r}")
            continue
        # Item 3 (CLAUDE.md NN14, P2/P3 gate review): amount_column/
        # date_column drive G6 reconciliation and the run headline -- a typo
        # or a renamed contract column here would otherwise surface only at
        # run time as a silently zeroed/None reconciliation figure
        # (orchestrator.populations.build_population). Caught at Skill load
        # instead, against the contract's OWN declared columns for this
        # population's source.
        contract_columns = skill.contract["sources"][src].get("columns", {})
        for col_key in ("amount_column", "date_column"):
            col = pop_cfg.get(col_key)
            if col and col not in contract_columns:
                violations.append(
                    f"populations.{source_name}.{col_key} {col!r} is not a column of "
                    f"contract source {src!r}"
                )

    plan_refs = _collect_all(populations, "ref") - {"audit_period"}
    for ref in plan_refs:
        if ref not in skill.references:
            violations.append(f"plan.yaml: unknown reference {ref!r}")

    # frame_tags (orchestrator/frames.py, CLAUDE.md build brief P4 perf fix): an
    # optional, declarative row-tagging block for /workspace/tne's snapshots --
    # every `source` must be a real contract source and every `from` value a
    # real population, exactly like a test's own population references above.
    for tag_name, tag_cfg in skill.plan.get("frame_tags", {}).items():
        tag_source = tag_cfg.get("source")
        if tag_source not in skill.contract.get("sources", {}):
            violations.append(f"frame_tags.{tag_name}: unknown source {tag_source!r}")
        for label, pop_name in tag_cfg.get("from", {}).items():
            if pop_name not in populations:
                violations.append(
                    f"frame_tags.{tag_name}.from.{label}: unknown population {pop_name!r}"
                )

    for i, test in enumerate(tests):
        test_id = test.get("test_id", f"tests[{i}]")
        if test_id in seen_test_ids:
            violations.append(f"tests: duplicate test_id {test_id!r}")
        seen_test_ids.add(test_id)

        if "not_testable" in test:
            continue

        primitive_name = test.get("primitive")
        params = test.get("params", {})
        flag = test.get("flag", "")
        if not flag.startswith("RF_"):
            violations.append(f"{test_id}: flag {flag!r} must start with 'RF_'")

        module = PRIMITIVES.get(primitive_name)
        custom_entry = skill.custom_primitives.get(primitive_name)
        if module is None and custom_entry is None:
            violations.append(f"{test_id}: unknown primitive {primitive_name!r}")
        else:
            schema = module.PARAMS_SCHEMA if module is not None else custom_entry["params_schema"]
            try:
                jsonschema.validate(params, schema)
            except jsonschema.ValidationError as exc:
                violations.append(f"{test_id}: params invalid for primitive {primitive_name!r}: {exc.message}")

            # The RF_* column that actually lands in the run's wide flags frame comes
            # from params.flag (each primitive defaults it independently), never from
            # this test-level declaration -- so a Skill author who sets one and not
            # the other gets a flag column that silently doesn't match what plan.yaml
            # says it is. Require them to agree wherever the primitive takes a single
            # "flag" param; a multi-flag primitive (e.g. split_detection) is exempt,
            # since one test-level name cannot cover several flag columns.
            schema_props = (schema or {}).get("properties", {})
            if "flag" in schema_props:
                if params.get("flag") != flag:
                    violations.append(
                        f"{test_id}: params.flag {params.get('flag')!r} must equal the "
                        f"test's flag {flag!r} (the flags frame column is named from params.flag)"
                    )

        for pop_key in ("population", "left_population", "right_population"):
            pop_name = params.get(pop_key)
            if pop_name is not None and pop_name not in populations:
                violations.append(f"{test_id}: unknown population {pop_name!r} (param {pop_key})")

        for tid in _collect_all(params, "threshold"):
            if tid not in thresholds:
                violations.append(f"{test_id}: unknown threshold id {tid!r}")

        for ref in _collect_all(params, "ref"):
            if ref not in skill.references:
                violations.append(f"{test_id}: unknown reference {ref!r}")

        known_metric_names |= set(params.get("metrics", {}).keys())
        if custom_entry is not None:
            # A custom primitive (CLAUDE.md §4.3, T6.1a-shaped) may compute fixed
            # metric names internally rather than reading a generic `metrics:`
            # config -- it declares them via produces_metrics so findings.yaml can
            # still be validated against real, produced metric names.
            known_metric_names |= set(custom_entry.get("produces_metrics", []))

    for tid, spec in thresholds.items():
        provenance = spec.get("provenance", {})
        ptype = provenance.get("type")
        if ptype == "policy" and not provenance.get("reference"):
            violations.append(f"thresholds.{tid}: provenance.type=policy requires a reference")
        if ptype == "analyst-set" and provenance.get("pending_policy_confirmation") is not True:
            violations.append(
                f"thresholds.{tid}: provenance.type=analyst-set must set pending_policy_confirmation: true"
            )

    known_threshold_ids = set(thresholds.keys())
    # B2 (CLAUDE.md P2/P3 gate review): flattened across every test, for the
    # monetary_basis cross-check below -- deliberately NOT restricted to
    # "this finding's own test_id" (that restriction is exactly the string-
    # prefix mistake B1 fixed at run time; at Skill-load time there is no
    # run-recorded metric->test mapping to use instead, so this check only
    # asks "is there ANY additive-AUD metric among this finding's
    # metrics_cited", the same question prioritise() asks at run time).
    all_additive_amount_names: set[str] = set()
    for names in plan_test_amount_metrics(tests).values():
        all_additive_amount_names |= names

    seen_finding_ids: set[str] = set()
    for finding in skill.findings.get("findings", []):
        fid = finding.get("id", "?")
        if fid in seen_finding_ids:
            violations.append(f"findings.{fid}: duplicate finding id")
        seen_finding_ids.add(fid)

        if finding.get("test_id") not in seen_test_ids:
            violations.append(f"findings.{fid}: test_id {finding.get('test_id')!r} is not a test in plan.yaml")

        cited = set(finding.get("metrics_cited", []))
        unknown_cited = cited - known_metric_names
        if unknown_cited:
            violations.append(
                f"findings.{fid}: metrics_cited {sorted(unknown_cited)} not produced by any test"
            )

        # thresholds_cited (item 7, CLAUDE.md P2/P3 gate review): a finding's
        # prose may quote a threshold's own VALUE (e.g. "$50") -- every id
        # must be a real thresholds.yaml entry, exactly like a threshold
        # referenced from `trigger`/`severity.when`, so a number in prose can
        # never silently drift from what the run actually enforces.
        cited_thresholds = set(finding.get("thresholds_cited", []))
        unknown_cited_thresholds = cited_thresholds - known_threshold_ids
        if unknown_cited_thresholds:
            violations.append(
                f"findings.{fid}: thresholds_cited {sorted(unknown_cited_thresholds)} not in thresholds.yaml"
            )

        # B2: monetary_basis must agree with whether this finding actually
        # cites a real additive-AUD amount metric -- 'none' with a dollar
        # figure, or a monetary basis with no dollar figure at all, is
        # exactly the kind of authoring mistake that produced a wrong
        # headline in the first place.
        monetary_basis = finding.get("monetary_basis")
        has_amount_metric = bool(all_additive_amount_names & cited)
        if monetary_basis == "none" and has_amount_metric:
            violations.append(
                f"findings.{fid}: monetary_basis is 'none' but metrics_cited includes an "
                f"additive-AUD metric -- set a real monetary_basis"
            )
        if monetary_basis in ("spend", "excess", "approved_not_spent") and not has_amount_metric:
            violations.append(
                f"findings.{fid}: monetary_basis is {monetary_basis!r} but metrics_cited has no "
                f"additive-AUD metric to be that spend/excess/approved-not-spent figure"
            )

        try:
            trigger_expr = compile_expr(
                finding["trigger"], known_metrics=known_metric_names, known_thresholds=known_threshold_ids
            )
        except ExpressionError as exc:
            violations.append(f"findings.{fid}: trigger invalid: {exc}")
            trigger_expr = None

        for j, rule in enumerate(finding.get("severity", [])):
            if "when" in rule:
                try:
                    compile_expr(
                        rule["when"], known_metrics=known_metric_names, known_thresholds=known_threshold_ids
                    )
                except ExpressionError as exc:
                    violations.append(f"findings.{fid}: severity[{j}].when invalid: {exc}")
            elif "else" not in rule:
                violations.append(f"findings.{fid}: severity[{j}] has neither 'when' nor 'else'")

        for template_field in ("observation", "recommendation"):
            placeholders = _template_placeholders(finding.get(template_field, ""))
            unknown_placeholders = placeholders - cited - cited_thresholds
            if unknown_placeholders:
                violations.append(
                    f"findings.{fid}: {template_field} references {sorted(unknown_placeholders)} "
                    f"not in metrics_cited or thresholds_cited"
                )

        del trigger_expr  # compiled only to validate; findings.py recompiles per-run

    if violations:
        raise SkillValidationError(violations)
