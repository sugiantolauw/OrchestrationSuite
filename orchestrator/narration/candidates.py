"""AI-proposed CANDIDATE findings (P6 WP N8, docs/specs/P6_narration_design.md
§5, §5.1, §5.4). The `find_candidates` task lets the `narrate` node's model
*propose* extra findings from this run's own computed test results
(CLAUDE.md §3 non-negotiable 2's amendment, "hybrid findings") -- it never
invents a number, never decides membership on its own, and never touches a
RULE finding's existence, numbers or severity. Everything a candidate needs
to exist as a real, addressable row (its identity, whether it duplicates a
rule finding, its monetary basis, its own amount at risk) is computed here,
in Python, from data this run already persisted -- `run_metrics`,
`findings`, `test_results` and `test_line_values` -- never re-read from a
raw source row (the same "never rows" discipline `orchestrator.narration.
payloads` documents).

Two passes, matching §5.1's own split between a PROSE gate (repairable) and
DATA gates (Python-only drops, never repaired):

  1. One `find_candidates` call, through the SAME generate/validate/repair/
     fallback loop every other narration task uses
     (`orchestrator.narration.runner._generate_item`) -- `validate_fn` here
     checks only prose validity (§3.3-§3.4: title has no placeholders,
     grammar, coverage of the candidate's OWN cited metrics, and so on).
     Invalid prose triggers one repair round; two failures fall back to
     **zero candidates** (§3.5's own fallback table: "candidates -> none" --
     there is no rule-authored template to fall back to, because a
     candidate does not exist until the model successfully proposes it).
  2. For every candidate the model DID successfully propose, the C-1..C-5
     data rules (§5.1) are applied in Python, silently dropping (never
     repairing) anything that fails one -- a candidate whose metrics do not
     satisfy the G10 anchor rule, that duplicates an already-decided
     candidate, or that would push the run over `NARRATION_MAX_CANDIDATES`.
     Survivors get their identity, monetary basis and headline eligibility
     computed here (never proposed by the model, §5.1) and are persisted via
     `write_candidates` with `candidate_status='candidate'`; their
     observation/recommendation/management_questions go through the SAME
     `narratives` pipeline a rule finding's prose does (target_kind=
     'candidate'), keyed by the SAME deterministic `narrative_id` scheme
     (`orchestrator.narration.runner.narrative_id`) so a reader can find them
     with no extra RunState field, exactly as CLAUDE.md §6.2 documents for
     rule findings.

`title`, `severity_reason` and `rationale` are stored directly on the
`finding_candidates` row (migration 011's DDL has no `narratives` link for
them) -- they are short, non-editable-at-this-stage strings, unlike the
observation/recommendation/questions triad an auditor may later accept,
reject or (post-acceptance, a later WP) edit.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from orchestrator.narration import runner
from orchestrator.narration.payloads import build_candidates_payload
from orchestrator.narration.placeholders import PlaceholderEntry
from orchestrator.narration.schemas import finding_candidates_schema
from orchestrator.narration.validate import validate_prose
from orchestrator.skills import plan_test_amount_metrics

__all__ = ["narrate_candidates"]

_VALID_SEVERITIES = ("High", "Medium", "Low")


# ── identity (§5.1's own table) ─────────────────────────────────────────────


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def rule_id_for(skill_prefix: str, producing_test_ids: list[str], metrics_cited: list[str]) -> str:
    """`<skill_prefix>.ai.<sha256(canonical({"tests":..., "metrics":...}))[:12]>`
    -- stable across runs and periods (rollforward, CLAUDE.md §4.8) because
    it is a pure function of WHICH tests and metrics this candidate is
    about, never of `run_id` or `generation`."""
    payload = {"tests": sorted(producing_test_ids), "metrics": sorted(metrics_cited)}
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:12]
    return f"{skill_prefix}.ai.{digest}"


def candidate_id_for(run_id: str, generation: int, rule_id: str) -> str:
    return hashlib.sha256(f"{run_id}|{generation}|{rule_id}".encode("utf-8")).hexdigest()[:24]


# ── monetary basis (§5.1: "derived by Python, never proposed by the model") ─


def _rule_basis_for_metric(name: str, findings: list[dict]) -> tuple[str | None, bool]:
    """Every RULE finding (never a candidate -- candidates cannot yet cite
    each other) that cites `name` in ITS OWN `metrics_cited`; returns
    `(the single agreed basis, False)`, `(None, False)` when no rule cites
    it, or `(None, True)` when two or more rules citing it disagree."""
    bases = {f.get("monetary_basis") for f in findings if name in (f.get("metrics_cited") or {})}
    bases.discard(None)
    if not bases:
        return None, False
    if len(bases) > 1:
        return None, True
    return next(iter(bases)), False


def _derive_monetary_basis(
    amount_metric_names: list[str], findings: list[dict], metric_kind_by_name: dict[str, str],
) -> tuple[str, str | None]:
    """§5.1's own three-step order, applied per cited additive-currency
    metric, then reduced to ONE basis for the whole candidate: if every
    metric that resolved to a real (non-'none') basis agrees, that is the
    candidate's basis; any genuine disagreement -- or no additive-currency
    metric at all -- gives 'none' (§5.1: "errs towards leaving an amount out
    of the headline rather than inventing a basis")."""
    if not amount_metric_names:
        return "none", None
    bases: set[str] = set()
    notes: list[str] = []
    for name in sorted(amount_metric_names):
        rule_basis, disagreement = _rule_basis_for_metric(name, findings)
        if disagreement:
            notes.append(f"rule findings citing {name!r} disagree on monetary_basis")
            continue
        if rule_basis is not None:
            bases.add(rule_basis)
            continue
        if metric_kind_by_name.get(name) == "excess":
            bases.add("excess")
            continue
        notes.append(f"no declared monetary basis for {name!r}; not in headline")
    if len(bases) == 1:
        return next(iter(bases)), (notes[0] if notes else None)
    if len(bases) > 1:
        return "none", f"cited amount metrics disagree on monetary basis: {sorted(bases)}"
    return "none", (notes[0] if notes else "no declared monetary basis for any cited amount metric; not in headline")


# ── headline eligibility, read from the PERSISTED test_line_values table
# (never re-derived from a raw row -- narrate() has no source access) ──────


def _headline_eligibility(
    basis: str, producing_test_ids: list[str], test_line_values_by_test: dict[str, list[dict]],
) -> tuple[bool, str | None]:
    if basis == "approved_not_spent":
        return False, (
            "approved_not_spent candidates are reported separately from the amount-at-risk headline, "
            "the same as an approved_not_spent rule finding (CLAUDE.md §0.2/§9 decisions)"
        )
    if basis != "excess" and basis != "spend":
        return False, "no monetary basis was derived for this candidate's cited metrics; not in the amount-at-risk headline"
    any_row = False
    any_priced = False
    for tid in producing_test_ids:
        for row in test_line_values_by_test.get(tid, []):
            any_row = True
            amount = row["spend_amount"] if basis == "spend" else row.get("excess_amount")
            if amount is not None:
                any_priced = True
                break
        if any_priced:
            break
    if not any_row:
        return False, "no test_line_values rows are recorded for this candidate's producing test(s)"
    if not any_priced:
        if basis == "excess":
            return False, (
                "the excess allocation this candidate's producing test(s) compute does not cover any "
                "of its lines -- never fails the run (CLAUDE.md P6 §5.1)"
            )
        return False, "no priced line is available for this candidate's producing test(s)"
    return True, None


# ── the two data lookups this module needs from plan.yaml, built once per
# call rather than imported from orchestrator.narration.payloads (which
# keeps them nested inside per-test dicts this module has no other use for) ─


def _metric_kind_by_name(plan_tests: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for test in plan_tests:
        if "not_testable" in test:
            continue
        for name, spec in ((test.get("params") or {}).get("metrics") or {}).items():
            kind = spec.get("kind")
            if kind:
                out[name] = kind
    return out


# BUG-R5-3 (independent review 2026-09-26): a candidate cited
# `t61d_dom_city_currency_conflict_rows` (value 0 -- zero conflicts) next to
# `daily_over_employees_dom` (value 1, a real per-diem exceedance), from the
# SAME test_id (T6.1d_dom), and the model's prose read "0 rows ... conflict,
# involving 1 employees" -- a category error: the conflict-rows metric is a
# standalone data-quality readout over the WHOLE population
# (`skills/tne_exco/custom.py:country_from_city_or_currency`, computed
# before `threshold_exceedance` ever filters to exceeding rows), not part of
# that primitive's own exceedance metric set, even though it is declared
# under the same test_id for reporting convenience. `population_size`/
# `pct_of_population` metrics are the one legitimate exception: a
# denominator or a rate is routinely cited alongside a count without itself
# needing to be non-zero (CLAUDE.md's own findings.yaml pattern, "{count} of
# {pct}% of {total}").
_CONTEXT_METRIC_KINDS = frozenset({"pct_of_population"})
_CONTEXT_METRIC_KEYS = frozenset({"population_size"})


def _metric_key_by_name(plan_tests: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for test in plan_tests:
        if "not_testable" in test:
            continue
        for name, spec in ((test.get("params") or {}).get("metrics") or {}).items():
            out[name] = spec.get("key") or name
    return out


def _is_context_metric(name: str, kind_by_name: dict[str, str], key_by_name: dict[str, str]) -> bool:
    return kind_by_name.get(name) in _CONTEXT_METRIC_KINDS or key_by_name.get(name) in _CONTEXT_METRIC_KEYS


def _candidate_metric_set_violations(
    cited: list[str], all_metrics_table: dict[str, PlaceholderEntry], metrics: dict[str, dict],
    kind_by_name: dict[str, str], key_by_name: dict[str, str],
) -> list[dict]:
    """Two deterministic, Python-only checks over a proposed candidate's OWN
    `metrics_cited` -- run as part of prose validation (§3.3-3.4's own
    validate/repair/fallback loop), so a violation here is recorded and
    repaired exactly like any other candidate validation failure, never a
    silent drop the way C-1..C-5 (data gates, below) are:

    - at least one cited "exception" metric (any metric that is not a
      recognised context/denominator one, `_is_context_metric`) must be
      non-zero, AND every such exception metric cited must be non-zero --
      a zero-valued exception metric riding along next to an unrelated
      non-zero one is exactly the shape that produced "0 rows ... conflict,
      involving 1 employees";
    - every cited metric's own `test_id` (`run_metrics` provenance) must
      agree -- metrics genuinely belonging to two different tests are two
      different matters, never one candidate."""
    violations: list[dict] = []
    exception_names = [
        n for n in cited if n in all_metrics_table and not _is_context_metric(n, kind_by_name, key_by_name)
    ]
    zero_names = sorted(n for n in exception_names if (all_metrics_table[n].value or 0) == 0)
    if not exception_names:
        violations.append(
            {"rule_id": "C-6", "field": "metrics_cited", "excerpt": "cites no exception metric"[:80]}
        )
    elif zero_names:
        violations.append(
            {"rule_id": "C-6", "field": "metrics_cited", "excerpt": f"zero-valued exception metric(s) {zero_names}"[:80]}
        )
    test_ids = sorted({metrics[n]["test_id"] for n in cited if n in metrics and metrics[n].get("test_id")})
    if len(test_ids) > 1:
        violations.append(
            {"rule_id": "C-7", "field": "metrics_cited", "excerpt": f"metrics from more than one test {test_ids}"[:80]}
        )
    return violations


# ── prose validation (C-6): the ONE thing that can trigger a repair round --
# every other rule below is a Python-only drop, applied after a valid
# response comes back (§5.1) ────────────────────────────────────────────────


def _field(
    text: str, table: dict[str, PlaceholderEntry], *, field: str, allowed_identifiers: set[str] = frozenset(),
    require_coverage: bool = False, required_placeholders: list[str] | None = None,
) -> list[dict]:
    result = validate_prose(
        text, table, field=field, origin="model", allowed_identifiers=allowed_identifiers,
        require_coverage=require_coverage, required_placeholders=required_placeholders,
    )
    return [
        {"rule_id": v.rule_id, "field": field, "excerpt": (v.text or v.message)[:80]}
        for v in result.violations
    ]


def allowed_identifiers_for_cited_metrics(
    cited: list[str], metrics: dict[str, dict], test_ident: dict[str, dict]
) -> set[str]:
    """The N-D1 allow-list for a candidate's own prose (§5.1): each cited
    metric's `test_id` (`run_metrics`'s own per-metric provenance) plus
    that test's `control_id`/`risk_id` (`test_ident`, built from
    `skill.plan`'s own tests -- CLAUDE.md §4.9). Public (not `_`-prefixed):
    `orchestrator.service._narrative_allowed_identifiers` re-validates a
    persisted `candidate`-kind narrative against this SAME computation
    (BUG-SYNTH-T1T2's fix, independent review round 3, 2026-09-25 -- a
    re-validation must build its allow-list identically to generation,
    never independently)."""
    ids: set[str] = set()
    for name in cited:
        test_id = (metrics.get(name) or {}).get("test_id")
        if not test_id:
            continue
        ids.add(test_id)
        ids.update(v for v in test_ident.get(test_id, {}).values() if v)
    return ids


def _validate_response(
    parsed: dict, all_metrics_table: dict[str, PlaceholderEntry], metrics: dict[str, dict],
    test_ident: dict[str, dict], kind_by_name: dict[str, str] | None = None, key_by_name: dict[str, str] | None = None,
) -> tuple[bool, list[dict]]:
    kind_by_name = kind_by_name or {}
    key_by_name = key_by_name or {}
    violations: list[dict] = []
    for item in parsed.get("candidates", []):
        cited = [n for n in (item.get("metrics_cited") or []) if isinstance(n, str)]
        table = {n: all_metrics_table[n] for n in cited if n in all_metrics_table}
        allowed = allowed_identifiers_for_cited_metrics(cited, metrics, test_ident)
        violations += _candidate_metric_set_violations(cited, all_metrics_table, metrics, kind_by_name, key_by_name)
        violations += _field(item.get("title", ""), table, field="candidate_title", allowed_identifiers=allowed)
        violations += _field(
            item.get("observation", ""), table, field="observation", allowed_identifiers=allowed,
            require_coverage=True, required_placeholders=[n for n in cited if n in table],
        )
        violations += _field(item.get("recommendation", ""), table, field="recommendation", allowed_identifiers=allowed)
        for q in item.get("management_questions", []):
            violations += _field(q, table, field="question", allowed_identifiers=allowed)
        violations += _field(item.get("severity_reason", ""), table, field="rationale", allowed_identifiers=allowed)
        violations += _field(item.get("rationale", ""), table, field="rationale", allowed_identifiers=allowed)
    return not violations, violations


# ── C-1..C-5 (§5.1): applied per proposed candidate, AFTER prose validation
# has already passed -- every failure here silently DROPS the one candidate,
# never repairs and never fails the run ─────────────────────────────────────


def _build_candidate_row(
    item: dict, *, run_id: str, generation: int, skill_id: str, engagement_id: str | None,
    metrics: dict[str, dict], rule_cited_metric_names: set[str], test_results_by_id: dict[str, dict],
    amount_metric_names: set[str], metric_kind_by_name: dict[str, str], findings: list[dict],
    decided_rule_ids: set[str], seen_rule_ids: set[str], test_line_values_by_test: dict[str, list[dict]],
    call_id: str,
) -> dict | None:
    cited = list(dict.fromkeys(n for n in (item.get("metrics_cited") or []) if isinstance(n, str)))

    # C-1: 1..8 entries, each a real, non-null, non-run-level metric this run computed.
    if not (1 <= len(cited) <= 8):
        return None
    resolved: dict[str, dict] = {}
    for name in cited:
        metric = metrics.get(name)
        if metric is None or metric.get("value") is None or name.startswith("run_"):
            return None
        resolved[name] = metric

    # C-2 (the G10 anchor): at least one cited metric is cited by NO rule
    # finding, its producing test has exception_units > 0, and its own
    # value > 0.
    anchor_ok = False
    for name in cited:
        if name in rule_cited_metric_names:
            continue
        test_id = resolved[name].get("test_id")
        result = test_results_by_id.get(test_id) if test_id else None
        if result is None:
            continue
        if (result.get("exception_units") or 0) > 0 and (resolved[name].get("value") or 0) > 0:
            anchor_ok = True
            break
    if not anchor_ok:
        return None

    producing_test_ids = sorted({resolved[n]["test_id"] for n in cited if resolved[n].get("test_id")})

    # C-3: every producing test is testable.
    for test_id in producing_test_ids:
        result = test_results_by_id.get(test_id)
        if result is None or result.get("status") == "not_testable":
            return None

    rule_id = rule_id_for(skill_id, producing_test_ids, cited)

    # C-4: not a rule/matter already decided this run, and not a duplicate
    # within this one batch (keeps the first).
    if rule_id in decided_rule_ids or rule_id in seen_rule_ids:
        return None

    proposed_severity = item.get("proposed_severity")
    if proposed_severity not in _VALID_SEVERITIES:
        return None

    amount_names = sorted(n for n in cited if n in amount_metric_names)
    exposure_amount = round(sum(resolved[n]["value"] for n in amount_names), 2) if amount_names else None
    monetary_basis, monetary_basis_note = _derive_monetary_basis(amount_names, findings, metric_kind_by_name)
    if amount_names:
        headline_eligible, headline_reason = _headline_eligibility(
            monetary_basis, producing_test_ids, test_line_values_by_test,
        )
    else:
        headline_eligible, headline_reason = False, "this candidate cites no additive amount metric"

    candidate_id = candidate_id_for(run_id, generation, rule_id)
    return {
        "candidate_id": candidate_id, "engagement_id": engagement_id, "skill_id": skill_id,
        "generation": generation, "rule_id": rule_id, "title": (item.get("title") or "").strip(),
        "metrics_cited": cited, "producing_test_ids": producing_test_ids,
        "proposed_severity": proposed_severity, "severity_reason": item.get("severity_reason"),
        "rationale": item.get("rationale"), "monetary_basis": monetary_basis,
        "monetary_basis_note": monetary_basis_note, "exposure_amount": exposure_amount,
        "headline_eligible": headline_eligible, "headline_ineligible_reason": headline_reason,
        "candidate_status": "candidate", "call_id": call_id,
        "observation": item.get("observation", ""), "recommendation": item.get("recommendation", ""),
        "management_questions": list(item.get("management_questions") or []),
    }


# ── entry point, called from orchestrator.nodes.narration.narrate ──────────


def narrate_candidates(
    rc: runner.RunnerContext, *, state, skill, metrics: dict[str, dict], findings: list[dict], max_candidates: int,
) -> tuple[list[dict], int]:
    """Returns `(persisted candidate rows, count of undecided candidates from
    an earlier generation this call superseded)`. Called only behind the
    `ai_proposed_findings_enabled` switch (the caller's own check, §1 #9) --
    this function itself has no config read, so it is honestly testable with
    the switch modelled entirely by whether the caller invokes it at all."""
    now = rc.clock()

    # §5.5 point 1 / §5.2: idempotent regardless of generation -- for
    # generation 0 this supersedes rows with generation < 0 (none, a no-op);
    # for a real regenerate it retires any still-undecided rows from every
    # earlier generation before this one proposes its own.
    superseded_count = rc.persistence.supersede_undecided(rc.run_id, below_generation=rc.generation, now=now)

    # C-4: "accepted, rejected" -- a merely SUPERSEDED row (never decided by
    # an auditor, just retired by a regenerate) does not block the same
    # matter from being re-proposed; only a genuine auditor decision does.
    decided = [c for c in rc.persistence.list_candidates(rc.run_id) if c["candidate_status"] in ("accepted", "rejected")]
    decided_rule_ids = {c["rule_id"] for c in decided}

    built = build_candidates_payload(skill=skill, state=state, metrics=metrics, findings=findings, decided_candidates=decided)
    if built is None:
        # §4.1/§5.1's own skip condition: no plan test has any exception at
        # all -- a clean run (G10) never even attempts a call.
        return [], superseded_count

    payload, tables_by_test = built
    all_metrics_table: dict[str, PlaceholderEntry] = {}
    for table in tables_by_test.values():
        all_metrics_table.update(table)
    metric_names = sorted(all_metrics_table)
    if not metric_names:
        return [], superseded_count

    plan_tests = skill.plan.get("tests", [])
    test_ident: dict[str, dict] = {}
    for test in plan_tests:
        tid = test.get("test_id")
        if tid:
            test_ident[tid] = {"control_id": test.get("control_id"), "risk_id": test.get("risk_id")}

    schema = finding_candidates_schema(metric_names, max_candidates)
    metric_kind_by_name = _metric_kind_by_name(plan_tests)
    metric_key_by_name = _metric_key_by_name(plan_tests)

    def validate_fn(parsed: dict) -> tuple[bool, list[dict]]:
        return _validate_response(parsed, all_metrics_table, metrics, test_ident, metric_kind_by_name, metric_key_by_name)

    outcome = runner._generate_item(
        rc, task="find_candidates", payload=payload, schema=schema,
        extra_params={"max_candidates": max_candidates}, validate_fn=validate_fn,
    )
    if outcome.origin not in ("model", "model_repaired"):
        # §3.5's own fallback table: "candidates -> none". There is nothing
        # to fall back TO -- a candidate only exists once the model has
        # successfully proposed it.
        return [], superseded_count

    rule_cited_metric_names: set[str] = set()
    for finding in findings:
        rule_cited_metric_names |= set((finding.get("metrics_cited") or {}).keys())

    test_results_by_id = {t["test_id"]: t for t in (getattr(state, "test_results", None) or [])}
    amount_metrics_by_test = plan_test_amount_metrics(plan_tests)
    amount_metric_names: set[str] = set()
    for names in amount_metrics_by_test.values():
        amount_metric_names |= names

    test_line_values_by_test: dict[str, list[dict]] = {}
    for row in rc.persistence.list_test_line_values(rc.run_id):
        test_line_values_by_test.setdefault(row["test_id"], []).append(row)

    call_id = outcome.call_ids[-1] if outcome.call_ids else None
    rows: list[dict] = []
    seen_rule_ids: set[str] = set()
    for item in (outcome.parsed or {}).get("candidates", []):
        if len(rows) >= max_candidates:
            break  # C-5: dropped with a reason -- simply not built.
        row = _build_candidate_row(
            item, run_id=rc.run_id, generation=rc.generation, skill_id=skill.skill_id,
            engagement_id=rc.engagement_id, metrics=metrics, rule_cited_metric_names=rule_cited_metric_names,
            test_results_by_id=test_results_by_id, amount_metric_names=amount_metric_names,
            metric_kind_by_name=metric_kind_by_name, findings=findings, decided_rule_ids=decided_rule_ids,
            seen_rule_ids=seen_rule_ids, test_line_values_by_test=test_line_values_by_test, call_id=call_id,
        )
        if row is None:
            continue
        seen_rule_ids.add(row["rule_id"])
        rows.append(row)

    if not rows:
        return [], superseded_count

    rc.persistence.write_candidates(
        rc.run_id,
        [{k: v for k, v in row.items() if k not in ("observation", "recommendation", "management_questions")} for row in rows],
        now=now,
    )

    for row in rows:
        table = {name: all_metrics_table[name] for name in row["metrics_cited"] if name in all_metrics_table}
        runner._persist(
            rc, target_kind="candidate", target_id=row["candidate_id"], field="observation", origin=outcome.origin,
            table=table, text=row["observation"], call_ids=outcome.call_ids,
            served_model_version=outcome.served_model_version, violations_payload=outcome.violations_payload,
        )
        runner._persist(
            rc, target_kind="candidate", target_id=row["candidate_id"], field="recommendation", origin=outcome.origin,
            table=table, text=row["recommendation"], call_ids=outcome.call_ids,
            served_model_version=outcome.served_model_version, violations_payload=outcome.violations_payload,
        )
        runner._persist(
            rc, target_kind="candidate", target_id=row["candidate_id"], field="management_questions",
            origin=outcome.origin, table=table, list_text=row["management_questions"], call_ids=outcome.call_ids,
            served_model_version=outcome.served_model_version, violations_payload=outcome.violations_payload,
        )

    return rows, superseded_count
