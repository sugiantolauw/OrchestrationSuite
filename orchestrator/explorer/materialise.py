"""Materialises a confirmed Explorer proposal into the standard Skill YAML
files (CLAUDE.md §4.4, §4.6; docs/specs/P6_P8_explorer_llm_design.md §4.11).

`materialise()` is pure and deterministic: given the same `effective`
canonical proposal (already narrowed to included, valid tests and their
findings -- that narrowing is `apply_plan_edits` + revalidation, WP6's
job, not this module's) and the same profile/sources, it emits byte-
identical YAML every time. It never reads a Skill's YAML back in --
`load_skill_from_ledger` (orchestrator.skills) does that, over exactly what
this module writes.

Nothing here writes `custom.py` or `workspace.py` -- Explorer cannot author
either (CLAUDE.md §4.4/§4.5: "Explorer does not generate code"); a
materialised Skill has none, which is also why `load_skill_from_ledger`
refuses any `.py` entry outright (defence in depth, not merely "we never
happen to write one")."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

import yaml

from orchestrator.explorer.currency import resolve_currency_column, resolve_currency_unit
from orchestrator.explorer.param_walk import iter_column_params

# ── identifiers (§4.11) ──────────────────────────────────────────────────


def _ns(effective: dict) -> str:
    canon = json.dumps(effective, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:8]


def _renumber(items: list[dict], key_field: str, prefix: str, width: int = 2) -> dict[str, str]:
    """`{original_key: "EX01", ...}` in proposal order."""
    return {item[key_field]: f"{prefix}{i + 1:0{width}d}" for i, item in enumerate(items)}


# ── plan.yaml `rule` text (catalogue.yaml, §4.11) ────────────────────────

_DIRECTION_WORDS = {
    "above": "above", "below": "below",
    "at_or_above": "at or above", "at_or_below": "at or below",
}


def _limit_ref(spec: dict | None) -> str:
    if isinstance(spec, dict) and "threshold" in spec:
        return "{" + spec["threshold"] + "}"
    if isinstance(spec, dict) and "column" in spec:
        return f"`{spec['column']}`"
    return "the limit"


def _rule_text(primitive: str, params: dict) -> str:
    if primitive == "threshold_exceedance":
        return (
            f"Rows where `{params.get('column')}` is {_DIRECTION_WORDS.get(params.get('direction'), params.get('direction'))} "
            f"{_limit_ref(params.get('limit'))}"
        )
    if primitive == "duplicate_detection":
        return f"Groups of rows sharing {params.get('key_columns')} with more than one member are flagged as duplicates"
    if primitive == "split_detection":
        return (
            f"Groups sharing {params.get('group_keys')} whose same-day or "
            f"{_limit_ref(params.get('window_days'))}-day-window total exceeds "
            f"{_limit_ref(params.get('aggregate_threshold'))} are flagged as potential split claims"
        )
    if primitive == "anti_join_gap":
        mode_word = "no" if params.get("mode") == "anti" else "a"
        return (
            f"Left-population rows with {mode_word} match in the right population, joined on "
            f"{params.get('left_keys')} = {params.get('right_keys')}, are flagged"
        )
    if primitive == "list_membership":
        negate_word = "not " if params.get("negate") else ""
        return f"Rows where `{params.get('column')}` is {negate_word}a member of the allowed list are flagged"
    if primitive == "date_lag":
        return (
            f"Rows where the days between `{params.get('start_column')}` and `{params.get('end_column')}` "
            f"is {_DIRECTION_WORDS.get(params.get('direction'), params.get('direction'))} "
            f"{_limit_ref(params.get('threshold'))}"
        )
    if primitive == "ratio_per_group":
        return (
            f"Rows where `{params.get('numerator_column')}` / `{params.get('denominator_column')}` is "
            f"{_DIRECTION_WORDS.get(params.get('direction'), params.get('direction'))} {_limit_ref(params.get('limit'))}"
        )
    if primitive == "attribute_missing":
        condition_words = {
            "is_null": "is null", "is_blank": "is blank", "not_equals": "does not equal the expected value",
        }
        return f"Rows where `{params.get('column')}` {condition_words.get(params.get('condition'), params.get('condition'))}"
    return f"See {primitive} primitive parameters"


def _threshold_refs_in_params(params: dict) -> list[str]:
    out = []
    for key in ("limit", "threshold", "window_days", "aggregate_threshold", "max_line"):
        spec = params.get(key)
        if isinstance(spec, dict) and "threshold" in spec:
            out.append(spec["threshold"])
    return out


def _threshold_placeholder_text(ids: list[str]) -> str:
    if not ids:
        return ""
    return "; ".join(f"{{{tid}}}" for tid in dict.fromkeys(ids))


# ── contract.yaml column collection ──────────────────────────────────────


def _profile_column(profile: dict, source: str, name: str) -> dict | None:
    for c in (profile.get(source) or {}).get("columns", []):
        if c["name"] == name:
            return c
    return None


def _contract_column_entry(colinfo: dict, *, allowed_values: list[str] | None = None) -> dict:
    entry: dict = {"type": colinfo["type"], "nullable": colinfo.get("null_count", 0) > 0}
    if colinfo.get("pii"):
        entry["pii"] = True
    if allowed_values:
        entry["allowed_values"] = list(allowed_values)
    return entry


def _resolve_source_kind_format(binding: dict) -> tuple[str, str | None]:
    kind = binding.get("kind", "local_file")
    fmt = binding.get("format") or ("uc_table" if kind == "uc_table" else "upload" if kind == "upload" else "csv")
    file_ = binding.get("file")
    return fmt, file_


# ── the entry point ────────────────────────────────────────────────────────


def materialise(
    effective: dict,
    *,
    profile: dict,
    run_sources: list[dict],
    run_id: str,
    confirmed_at: str,
    owner: str,
    audit_timezone: str,
) -> dict[str, bytes]:
    """`run_sources`: every bound run source, as `{"name", "kind", "format",
    "file"}` (kind: `uc_table` | `upload` | `local_file`; `format`/`file`
    per orchestrator.schemas.contract.schema.json's per-source shape --
    §4.2's `options.explorer.sources`). `effective` is the ALREADY-narrowed
    canonical proposal (only included, valid tests and their findings;
    §4.10's `apply_plan_edits` + revalidation is the caller's job)."""

    ns = _ns(effective)
    test_ids = _renumber(effective.get("tests", []), "key", "EX")
    finding_ids = _renumber(effective.get("findings", []), "key", "F")

    kept_test_keys = set(test_ids)
    kept_risk_keys = {t["risk_key"] for t in effective.get("tests", []) if t["key"] in kept_test_keys}
    kept_control_keys = {t["control_key"] for t in effective.get("tests", []) if t["key"] in kept_test_keys}
    kept_risks = [r for r in effective.get("risks", []) if r["key"] in kept_risk_keys]
    kept_controls = [c for c in effective.get("controls", []) if c["key"] in kept_control_keys]
    risk_ids = _renumber(kept_risks, "key", f"RSK-X-{ns}-")
    control_ids = _renumber(kept_controls, "key", f"CTL-X-{ns}-")

    kept_population_keys: set[str] = set()
    for t in effective.get("tests", []):
        if t["key"] not in kept_test_keys:
            continue
        for pop_param in ("population", "left_population", "right_population"):
            k = t.get("params", {}).get(pop_param)
            if k:
                kept_population_keys.add(k)
    kept_populations = [p for p in effective.get("populations", []) if p["key"] in kept_population_keys]

    sources_by_name = {s["source"]: s for s in effective.get("sources", [])}
    run_source_names = [b["name"] for b in run_sources]

    # ── threshold used_by + currency resolution ─────────────────────────
    used_by: dict[str, list[str]] = {}
    resolved_currency: dict[str, str] = {}
    for t in effective.get("tests", []):
        if t["key"] not in kept_test_keys:
            continue
        tid = test_ids[t["key"]]
        params = t.get("params", {})
        pop_key = params.get("population") or params.get("left_population")
        pop = next((p for p in effective.get("populations", []) if p["key"] == pop_key), None)
        source = pop["source"] if pop else None
        for thresh_id in _threshold_refs_in_params(params):
            used_by.setdefault(thresh_id, [])
            if tid not in used_by[thresh_id]:
                used_by[thresh_id].append(tid)
        for name, spec in (params.get("metrics") or {}).items():
            if spec.get("unit") == "currency" and source and source not in resolved_currency:
                code = resolve_currency_unit(source, profile)
                if code:
                    resolved_currency.setdefault(source, code)
        for thresh_id in _threshold_refs_in_params(params):
            th = next((x for x in effective.get("thresholds", []) if x["id"] == thresh_id), None)
            if th and th.get("unit") == "currency" and source:
                code = resolve_currency_unit(source, profile)
                if code:
                    resolved_currency.setdefault(thresh_id + "|" + source, code)
    for f in effective.get("findings", []):
        if f.get("test_key") not in kept_test_keys:
            continue
        tid = test_ids[f["test_key"]]
        for thresh_id in f.get("thresholds_cited", []):
            used_by.setdefault(thresh_id, [])
            if tid not in used_by[thresh_id]:
                used_by[thresh_id].append(tid)

    kept_threshold_ids = set(used_by)
    kept_thresholds = [th for th in effective.get("thresholds", []) if th["id"] in kept_threshold_ids]

    def _threshold_unit(th: dict) -> str:
        if th.get("unit") != "currency":
            return th["unit"]
        for key, code in resolved_currency.items():
            if key.startswith(th["id"] + "|"):
                return code
        # Fallback: any test's population currency (deterministic first-match).
        for t in effective.get("tests", []):
            if th["id"] in _threshold_refs_in_params(t.get("params", {})):
                pop_key = t["params"].get("population") or t["params"].get("left_population")
                pop = next((p for p in effective.get("populations", []) if p["key"] == pop_key), None)
                if pop and pop["source"] in resolved_currency:
                    return resolved_currency[pop["source"]]
        return th["unit"]

    # ── manifest.yaml ─────────────────────────────────────────────────
    manifest = {
        "id": f"EXPLORER-{run_id}",
        "name": effective.get("skill_name"),
        "domain": effective.get("domain"),
        "version": "run",
        "owner": owner,
        "status": "draft",
        "description": effective.get("summary"),
    }

    # ── contract.yaml ─────────────────────────────────────────────────
    used_columns_by_source: dict[str, set[str]] = {}
    for p in kept_populations:
        used_columns_by_source.setdefault(p["source"], set())
        for _loc, col in _iter_filter_columns(p.get("filters", [])):
            used_columns_by_source[p["source"]].add(col)
    for t in effective.get("tests", []):
        if t["key"] not in kept_test_keys:
            continue
        pop_key = t["params"].get("population") or t["params"].get("left_population")
        pop = next((p for p in effective.get("populations", []) if p["key"] == pop_key), None)
        source = pop["source"] if pop else None
        if source is None:
            continue
        for _loc, col in iter_column_params(t["params"]):
            used_columns_by_source.setdefault(source, set()).add(col)
        # anti_join_gap's right side
        right_pop_key = t["params"].get("right_population")
        if right_pop_key:
            right_pop = next((p for p in effective.get("populations", []) if p["key"] == right_pop_key), None)
            if right_pop:
                used_columns_by_source.setdefault(right_pop["source"], set())
                for k in t["params"].get("right_keys") or []:
                    used_columns_by_source[right_pop["source"]].add(k)
                for k in t["params"].get("carry_right_columns") or []:
                    used_columns_by_source[right_pop["source"]].add(k)

    contract_sources: dict[str, dict] = {}
    for binding in run_sources:
        name = binding["name"]
        fmt, file_ = _resolve_source_kind_format(binding)
        s = sources_by_name.get(name, {})
        columns_wanted = set(used_columns_by_source.get(name, set()))
        for col in (s.get("amount_column"), s.get("date_column")):
            if col:
                columns_wanted.add(col)
        for col in s.get("entry_key") or []:
            columns_wanted.add(col)
        # Independent review round 2 (BUG-EXPLORER-2, live regression): every
        # currency_code-classified column still belongs in the contract (so
        # it is readable/inspectable), but the allowed_values CONSTRAINT
        # below must land on the SAME column resolve_currency_unit actually
        # evidenced -- previously this was just the LAST currency_code
        # column in profile order, which could be a second, non-evidenced,
        # multi-valued "noise" column (e.g. a per-line transaction-FX
        # currency alongside the single-valued reimbursement currency), and
        # writing an `allowed_values: [AUD]` constraint onto THAT column
        # failed every real run with a genuine ContractViolation.
        currency_col = resolve_currency_column(name, profile)
        for c in (profile.get(name) or {}).get("columns", []):
            if c.get("semantic_type") == "currency_code":
                columns_wanted.add(c["name"])

        columns: dict[str, dict] = {}
        for col_name in sorted(columns_wanted):
            colinfo = _profile_column(profile, name, col_name)
            if colinfo is None:
                continue
            allowed = None
            if col_name == currency_col:
                code = resolved_currency.get(name)
                if code:
                    allowed = [code]
            columns[col_name] = _contract_column_entry(colinfo, allowed_values=allowed)

        source_entry: dict = {"format": fmt, "columns": columns}
        if file_:
            source_entry["file"] = file_
        if s.get("entry_key"):
            source_entry["entry_key"] = list(s["entry_key"])
        contract_sources[name] = source_entry

    contract = {"timezone": audit_timezone, "sources": contract_sources}

    # ── plan.yaml ────────────────────────────────────────────────────
    populations_yaml: dict[str, dict] = {}
    for name in run_source_names:
        s = sources_by_name.get(name, {})
        raw: dict = {"source": name}
        if s.get("amount_column"):
            raw["amount_column"] = s["amount_column"]
        if s.get("date_column"):
            raw["date_column"] = s["date_column"]
        populations_yaml[f"raw_{name}"] = raw

    for p in kept_populations:
        s = sources_by_name.get(p["source"], {})
        pop_yaml: dict = {"source": p["source"]}
        if p.get("filters"):
            pop_yaml["filters"] = p["filters"]
        if s.get("amount_column"):
            pop_yaml["amount_column"] = s["amount_column"]
        if s.get("date_column"):
            pop_yaml["date_column"] = s["date_column"]
        populations_yaml[p["key"]] = pop_yaml

    tests_yaml: list[dict] = []
    for t in effective.get("tests", []):
        if t["key"] not in kept_test_keys:
            continue
        tid = test_ids[t["key"]]
        params = dict(t["params"])
        metrics = params.get("metrics")
        if metrics:
            new_metrics = {}
            for mname, mspec in metrics.items():
                mspec = dict(mspec)
                if mspec.get("unit") == "currency":
                    pop_key = t["params"].get("population") or t["params"].get("left_population")
                    pop = next((p for p in effective.get("populations", []) if p["key"] == pop_key), None)
                    if pop and pop["source"] in resolved_currency:
                        mspec["unit"] = resolved_currency[pop["source"]]
                new_metrics[mname] = mspec
            params["metrics"] = new_metrics
        # A `{"threshold": id}` reference (limit/threshold/window_days/...)
        # keeps its proposal id as-is -- unit resolution for a currency
        # threshold lives entirely on thresholds.yaml (`_threshold_unit`
        # below), never duplicated onto the test's own params.
        flag = f"RF_{tid}"
        entry: dict = {
            "test_id": tid,
            "control_id": control_ids.get(t["control_key"], t["control_key"]),
            "risk_id": risk_ids.get(t["risk_key"], t["risk_key"]),
            "assertion": t["assertion"],
            "control_objective": t["control_objective"],
            "primitive": t["primitive"],
        }
        if t["primitive"] == "split_detection":
            entry["flag"] = flag
            params["flag_same_day"] = f"{flag}_SAME_DAY"
            params["flag_window"] = f"{flag}_WINDOW"
        else:
            entry["flag"] = flag
            params["flag"] = flag
        entry["params"] = params
        tests_yaml.append(entry)

    plan = {"populations": populations_yaml, "tests": tests_yaml}

    # ── findings.yaml ────────────────────────────────────────────────
    findings_yaml: list[dict] = []
    for f in effective.get("findings", []):
        if f.get("test_key") not in kept_test_keys:
            continue
        entry = {
            "id": finding_ids[f["key"]],
            "test_id": test_ids[f["test_key"]],
            "title": f["title"],
            "trigger": f["trigger"],
            "severity": f["severity"],
            "metrics_cited": f["metrics_cited"],
            "thresholds_cited": f.get("thresholds_cited", []),
            "monetary_basis": f["monetary_basis"],
            "observation": f["observation"],
            "recommendation": f["recommendation"],
        }
        if f.get("management_questions"):
            entry["management_questions"] = f["management_questions"]
        findings_yaml.append(entry)

    # ── thresholds.yaml ──────────────────────────────────────────────
    thresholds_yaml: dict[str, dict] = {}
    for th in kept_thresholds:
        thresholds_yaml[th["id"]] = {
            "value": th["value"],
            "unit": _threshold_unit(th),
            "description": th["description"],
            "used_by": sorted(used_by.get(th["id"], [])),
            "effective_date": confirmed_at[:10],
            "provenance": {"type": "analyst-set", "pending_policy_confirmation": True},
        }

    # ── risk_control.yaml ────────────────────────────────────────────
    controls_yaml = []
    for c in kept_controls:
        controls_yaml.append({
            "control_id": control_ids[c["key"]],
            "risk_id": risk_ids.get(c["risk_key"], c["risk_key"]),
            "title": c["title"],
            "description": c["description"],
            "type": c.get("type"),
            "tests": sorted(
                test_ids[t["key"]] for t in effective.get("tests", [])
                if t["key"] in kept_test_keys and t["control_key"] == c["key"]
            ),
        })
    risks_yaml = []
    for r in kept_risks:
        risks_yaml.append({
            "risk_id": risk_ids[r["key"]],
            "title": r["title"],
            "description": r["description"],
        })
    risk_control = {"controls": controls_yaml, "risks": risks_yaml}

    # ── catalogue.yaml ───────────────────────────────────────────────
    catalogue_tests = []
    for t in effective.get("tests", []):
        if t["key"] not in kept_test_keys:
            continue
        pop_key = t["params"].get("population") or t["params"].get("left_population")
        pop = next((p for p in effective.get("populations", []) if p["key"] == pop_key), None)
        catalogue_tests.append({
            "test_id": test_ids[t["key"]],
            "category": effective.get("domain"),
            "test_name": t["name"],
            "control_objective": t["control_objective"],
            "population": pop["description"] if pop else "",
            "rule": _rule_text(t["primitive"], t["params"]),
            "threshold": _threshold_placeholder_text(_threshold_refs_in_params(t["params"])),
        })
    catalogue = {"tests": catalogue_tests}

    files = {
        "manifest.yaml": manifest,
        "contract.yaml": contract,
        "plan.yaml": plan,
        "findings.yaml": {"findings": findings_yaml},
        "thresholds.yaml": thresholds_yaml,
        "risk_control.yaml": risk_control,
        "catalogue.yaml": catalogue,
    }
    return {
        name: yaml.safe_dump(content, sort_keys=False, allow_unicode=True).encode("utf-8")
        for name, content in files.items()
    }


def _iter_filter_columns(filters: list[dict]):
    for flt in filters or []:
        if "any_of" in flt:
            yield from _iter_filter_columns(flt["any_of"])
        elif "all_of" in flt:
            yield from _iter_filter_columns(flt["all_of"])
        elif "column" in flt:
            yield "filter", flt["column"]


# ── V-Z1: the belt-and-braces final check ───────────────────────────────
#
# "Materialise the valid subset and run the existing load_skill and
# validate_skill. Any violation traceable to a test key greys that test.
# An untraceable violation fails the proposal." (§4.7) Deliberately kept
# separate from `validate_proposal` (orchestrator.explorer.validate):
# V-Z1 needs materialise()'s own id renumbering (EX01... -> the original
# proposal key) to attribute a violation back to a key the auditor
# recognises, so it can only run AFTER materialisation, never before.


def check_materialised_skill(
    effective: dict,
    *,
    profile: dict,
    run_sources: list[dict],
    run_id: str,
    confirmed_at: str,
    owner: str,
    audit_timezone: str,
) -> dict[str, Any]:
    """Returns `{"proposal_errors": [str, ...], "test_violations":
    {original_test_key: [str, ...]}}` -- empty of both means V-Z1 passed.
    This should never fire in practice (every violation it could catch is
    already one of validate.py's own rules); it exists because
    `load_skill`/`validate_skill` are the ground truth every OTHER Skill
    is held to, and a proposal that passed every named rule but still
    fails them would otherwise materialise silently."""
    from orchestrator.skills import SkillValidationError, load_skill

    test_ids = _renumber(effective.get("tests", []), "key", "EX")
    reverse = {v: k for k, v in test_ids.items()}

    files = materialise(
        effective, profile=profile, run_sources=run_sources, run_id=run_id,
        confirmed_at=confirmed_at, owner=owner, audit_timezone=audit_timezone,
    )
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp)
        for name, content in files.items():
            (skill_dir / name).write_bytes(content)
        try:
            skill = load_skill(skill_dir)
            skill.validate()
        except SkillValidationError as exc:
            proposal_errors: list[str] = []
            test_violations: dict[str, list[str]] = {}
            for v in exc.violations:
                matched_key = next(
                    (key for tid, key in reverse.items() if v.startswith(f"{tid}:") or f" {tid}:" in v),
                    None,
                )
                if matched_key:
                    test_violations.setdefault(matched_key, []).append(v)
                else:
                    proposal_errors.append(v)
            return {"proposal_errors": proposal_errors, "test_violations": test_violations}
    return {"proposal_errors": [], "test_violations": {}}
