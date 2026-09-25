"""docs/specs/P7_mapping_authoring_design.md §2.1/§2.2: the Skill authoring
kit's checks. Two kinds:

- Checks REUSED as-is by calling existing code (`orchestrator.skills.
  validate_skill`, `orchestrator.explorer.validate._check_no_code_substrings`)
  -- never re-implemented here.
- Checks MOVED here because their logic previously lived only in
  `tests/test_g8_thresholds.py` and `tests/test_skill_content_lock.py`,
  hardwired to `skills/tne_exco` -- now generic functions any Skill
  directory can call, with those two test files reduced to calling them
  against SKILL-001 and asserting no violations (§2.1's "Moved into
  orchestrator/" row).

The one check §2.1 lists as "New" -- the risk/control referential check
(every plan.yaml control_id/risk_id exists in risk_control.yaml) -- is
added to `orchestrator.skills.validate_skill` itself by the parallel
column-mapping lane (batch B1a), so it is already covered by
`check_semantic_rules` below once that lands; nothing here duplicates it."""

from __future__ import annotations

import string
from pathlib import Path

import yaml

from orchestrator.authoring.report import ValidationReport
from orchestrator.expr import compile_expr
from orchestrator.explorer.validate import _check_no_code_substrings
from orchestrator.findings import build_findings
from orchestrator.skills import (
    _REQUIRED_FILES,
    _SCHEMAS,
    _collect_all,
    _load_yaml_validated,
    Skill,
    SkillValidationError,
    load_skill,
    validate_skill,
)

_FORMATTER = string.Formatter()
_MIN_PLANTS_PER_TEST = 15


def check_required_files_and_schemas(skill_dir: Path) -> list[str]:
    """Collect-all mode over every required Skill file's own JSON Schema --
    unlike `orchestrator.skills.load_skill`, which raises
    `SkillValidationError` on the FIRST failing file, this runs every file's
    schema check and returns every violation across all of them (§2.2 item
    2: "every file's schema errors in one report, not the first raised")."""
    violations: list[str] = []
    for name in _REQUIRED_FILES:
        path = skill_dir / name
        if not path.is_file():
            violations.append(f"missing required Skill file: {name}")
            continue
        schema = _SCHEMAS.get(name.removesuffix(".yaml"))
        if schema is None:
            # risk_control.yaml has no JSON Schema (orchestrator.skills
            # never registered one -- it is read as raw YAML by the
            # referential check inside validate_skill once B1a lands).
            # Still worth a parse check here so a malformed file is caught
            # by name rather than surfacing as an opaque failure later.
            try:
                yaml.safe_load(path.read_text())
            except yaml.YAMLError as exc:
                violations.append(f"{name}: could not parse YAML: {exc}")
            continue
        try:
            _load_yaml_validated(path, schema)
        except SkillValidationError as exc:
            violations.extend(exc.violations)
    return violations


def check_semantic_rules(skill: Skill) -> list[str]:
    """Reuses `orchestrator.skills.validate_skill` as-is (§2.1) -- every
    population/reference/primitive-param/threshold/findings-template rule
    it already enforces, plus the risk/control referential check once B1a
    adds it."""
    try:
        validate_skill(skill)
        return []
    except SkillValidationError as exc:
        return list(exc.violations)


def _catalogue_dict(skill: Skill) -> dict | None:
    path = skill.skill_dir / "catalogue.yaml"
    if not path.is_file():
        return None
    return yaml.safe_load(path.read_text()) or {}


def check_g8_thresholds(skill: Skill) -> list[str]:
    """Moved from `tests/test_g8_thresholds.py` (§2.1): catalogue threshold
    text renders against `thresholds.yaml`'s own values, every threshold
    referenced by `plan.yaml`/`findings.yaml` exists, every `thresholds.yaml`
    entry is referenced somewhere, every analyst-set threshold is flagged
    `pending_policy_confirmation: true` (every policy one carries a
    reference), and a finding whose matched severity rule consulted an
    analyst-set threshold is labelled `analyst_set_severity`. A Skill with
    no `catalogue.yaml` skips the catalogue-text checks (that file is
    SKILL-001-specific UI/PPTX methodology text, not a required Skill file)."""
    violations: list[str] = []
    thr_values = {tid: spec["value"] for tid, spec in skill.thresholds.items()}
    catalogue = _catalogue_dict(skill)

    catalogue_refs: set[str] = set()
    if catalogue is not None:
        for entry in catalogue.get("tests", []):
            placeholders = {fn for _, fn, _, _ in _FORMATTER.parse(entry["threshold"]) if fn}
            catalogue_refs |= placeholders
            unknown = placeholders - set(thr_values)
            if unknown:
                violations.append(
                    f"catalogue {entry.get('test_id', '?')}: threshold references unknown id(s) {sorted(unknown)}"
                )
                continue
            try:
                entry["threshold"].format(**thr_values)
            except KeyError as exc:
                violations.append(f"catalogue {entry.get('test_id', '?')}: threshold text does not render: {exc}")

    plan_refs = _collect_all(skill.plan, "threshold")
    findings_refs: set[str] = set()
    for finding in skill.findings.get("findings", []):
        findings_refs |= compile_expr(finding["trigger"]).threshold_ids
        for rule in finding.get("severity", []):
            if "when" in rule:
                findings_refs |= compile_expr(rule["when"]).threshold_ids
        findings_refs |= set(finding.get("thresholds_cited", []))

    all_refs = plan_refs | findings_refs
    unknown_refs = all_refs - set(skill.thresholds)
    if unknown_refs:
        violations.append(f"unknown threshold id(s) referenced: {sorted(unknown_refs)}")

    referenced = plan_refs | findings_refs | catalogue_refs
    unused = set(skill.thresholds) - referenced
    if unused:
        violations.append(f"thresholds.yaml entries never referenced: {sorted(unused)}")

    for tid, spec in skill.thresholds.items():
        provenance = spec["provenance"]
        if provenance["type"] == "analyst-set":
            if provenance.get("pending_policy_confirmation") is not True:
                violations.append(f"thresholds.{tid}: provenance.type=analyst-set must set pending_policy_confirmation: true")
        elif not provenance.get("reference"):
            violations.append(f"thresholds.{tid}: provenance.type=policy requires a reference")

    violations.extend(_check_severity_labelling(skill))
    return violations


def _check_severity_labelling(skill: Skill) -> list[str]:
    """Builds findings with every declared metric maxed out (so every
    trigger fires and every severity rule's largest `when` branch matches),
    then asserts every threshold_ref's recorded provenance_type matches
    thresholds.yaml, and every severity_basis is one of the two values
    `orchestrator.findings.build_findings` ever produces -- the same
    property `tests/test_g8_thresholds.py::
    test_findings_with_analyst_set_matched_severity_are_labelled` checked
    directly against SKILL-001, generalised to any Skill's own tests/
    findings."""
    metrics: dict[str, dict] = {}
    for test in skill.plan.get("tests", []):
        for name, spec in test.get("params", {}).get("metrics", {}).items():
            unit = spec.get("unit") or ("%" if "pct" in name else "count")
            metrics[name] = {"value": 99999, "unit": unit, "source_ref": {}}
    if not metrics:
        return []
    findings = build_findings(skill, run_id="authoring-g8-check", metrics=metrics)
    violations: list[str] = []
    thresholds = skill.thresholds
    for f in findings:
        for ref in f["threshold_refs"]:
            if thresholds[ref["id"]]["provenance"]["type"] != ref["provenance_type"]:
                violations.append(f"findings.{f['id']}: threshold_refs provenance mismatch for {ref['id']}")
        if f["severity_basis"] not in ("fixed", "threshold"):
            violations.append(f"findings.{f['id']}: unexpected severity_basis {f['severity_basis']!r}")
    return violations


def check_content_lock(skill_dir: Path) -> tuple[list[str], bool]:
    """Moved from `tests/test_skill_content_lock.py` (§2.1): a repo Skill's
    `content.lock` ({version, content_hash}) must still match its current
    manifest version and re-hashed content -- a missed version bump after a
    content edit breaks every fresh run of that Skill on a shared ledger
    (BUG-SKILLVER-1). Returns `(violations, has_lock)`: no lock file at all
    is `([], False)`, not a violation -- §2.2 item 4 wants that reported as
    a WARNING ("a draft Skill with no lock is a warning, not an error"), a
    decision only the caller (validate_skill_dir) can make since this
    function has no ValidationReport to add a warning to."""
    lock_path = skill_dir / "content.lock"
    if not lock_path.is_file():
        return [], False
    lock = yaml.safe_load(lock_path.read_text())
    skill = load_skill(skill_dir)
    violations: list[str] = []
    if skill.version != lock["version"]:
        violations.append(
            f"content.lock pins version {lock['version']!r} but manifest.yaml now says {skill.version!r} "
            f"-- regenerate content.lock for the new version"
        )
    if skill.content_hash != lock["content_hash"]:
        violations.append(
            f"this Skill's content changed under version {skill.version!r} without a version bump "
            f"(content_hash is now {skill.content_hash!r}, content.lock still pins {lock['content_hash']!r}) "
            f"-- bump the version and update the lock"
        )
    return violations, True


def check_no_code_substrings(skill: Skill) -> list[str]:
    """Reuses `orchestrator.explorer.validate._check_no_code_substrings`
    (V-P3, §2.1) over every prose field `findings.yaml` carries -- title,
    observation, recommendation, management_questions -- so an authored
    Skill's prose is held to the same "no SQL/imports/markdown fences"
    bar an Explorer proposal already is."""
    violations: list[str] = []
    for finding in skill.findings.get("findings", []):
        fid = finding.get("id", "?")
        for field_name in ("title", "observation", "recommendation"):
            hit = _check_no_code_substrings(finding.get(field_name, "") or "")
            if hit:
                violations.append(f"findings.{fid}.{field_name}: {hit}")
        for i, q in enumerate(finding.get("management_questions", [])):
            hit = _check_no_code_substrings(q)
            if hit:
                violations.append(f"findings.{fid}.management_questions[{i}]: {hit}")
    return violations


def check_code_presence(skill_dir: Path) -> list[str]:
    """§2.2 item 6: custom.py/workspace.py present -> a WARNING (the
    caller decides that, same as check_content_lock) naming which file --
    a Skill with either is no longer YAML-only work and needs code review."""
    warnings: list[str] = []
    for name in ("custom.py", "workspace.py"):
        if (skill_dir / name).is_file():
            warnings.append(f"{name} present -- contains code, needs code review; not a YAML-only Skill")
    return warnings


def check_plants_coverage(plants: dict | None) -> list[str]:
    """§2.2 item 7: warn below 15 plants and 15 near-miss negatives per
    scorable test -- the SKILL-001 precedent (docs/specs/
    SKILL-001_test_specification.md). `plants` is an already-loaded
    plants.yaml document, or None when no sidecar was given."""
    if not plants:
        return []
    warnings: list[str] = []
    for test_id, block in plants.get("tests", {}).items():
        if test_id.startswith("_"):
            continue
        entries = block.get("rows") if block.get("scoring_unit") == "row" else block.get("groups")
        entries = entries or []
        n_plants = sum(1 for e in entries if e.get("exception"))
        n_negatives = sum(1 for e in entries if not e.get("exception"))
        if n_plants < _MIN_PLANTS_PER_TEST or n_negatives < _MIN_PLANTS_PER_TEST:
            warnings.append(
                f"{test_id}: {n_plants} plant(s) / {n_negatives} near-miss negative(s) -- below the "
                f"{_MIN_PLANTS_PER_TEST}/{_MIN_PLANTS_PER_TEST} SKILL-001 precedent"
            )
    return warnings


def validate_skill_dir(
    path: str | Path, *, fixtures: str | Path | None = None, plants: str | Path | None = None
) -> ValidationReport:
    """§2.2's main entry point. Runs the checks in order, stopping only
    where a later check cannot run safely without an earlier one (required
    files/schemas, then `load_skill`). `fixtures`/`plants` together add a
    dry-run Surface 2 scoring pass (item 8) -- generated fixture data
    against the sidecar oracle, never one without the other."""
    skill_dir = Path(path)
    report = ValidationReport()

    file_violations = check_required_files_and_schemas(skill_dir)
    report.add_check("required files and schemas", file_violations)
    if file_violations:
        return report

    try:
        skill = load_skill(skill_dir)
    except SkillValidationError as exc:
        report.add_check("load_skill", list(exc.violations))
        return report
    report.add_check("load_skill", [])

    report.add_check("validate_skill (semantic rules)", check_semantic_rules(skill))
    report.add_check("G8 threshold provenance", check_g8_thresholds(skill))

    lock_violations, has_lock = check_content_lock(skill_dir)
    if has_lock:
        report.add_check("content.lock", lock_violations)
    else:
        report.add_check("content.lock", ["no content.lock -- draft Skill, not pinned to a version yet"], warning=True)

    report.add_check("prose templates (no code substrings)", check_no_code_substrings(skill))
    report.add_check("code presence (custom.py / workspace.py)", check_code_presence(skill_dir), warning=True)

    plants_doc = None
    if plants is not None:
        plants_doc = yaml.safe_load(Path(plants).read_text())
    report.add_check("plants coverage", check_plants_coverage(plants_doc), warning=True)

    if fixtures is not None and plants is not None:
        # Lazy import (agreed with lane K's parallel batch B2a/B2b split,
        # §4 batch table): orchestrator.authoring.score is a B2b file, so a
        # B2a-only checkout can still import and use this module -- the
        # import only executes when a caller actually passes both
        # fixtures and plants.
        from orchestrator.authoring.score import score_fixtures

        scores = score_fixtures(skill_dir, plants, fixtures)
        dry_run_violations: list[str] = []
        for test_id, score in sorted(scores.items()):
            if score.precision is not None and score.precision < 0.98:
                dry_run_violations.append(f"{test_id}: precision {score.precision:.3f} < 0.98")
            if score.recall is not None and score.recall < 0.95:
                dry_run_violations.append(f"{test_id}: recall {score.recall:.3f} < 0.95")
        report.add_check("dry-run Surface 2 (precision >= 0.98, recall >= 0.95)", dry_run_violations)

    return report
