"""§6.4's `effective_prose` resolver (P6 WP N10, docs/specs/
P6_narration_design.md §6.4): "the ONLY way the UI, XLSX and PPTX read
prose." A `narratives` row's `template_text` is stored UNRENDERED, with
typed placeholders (`orchestrator.narration.runner`'s own module docstring:
"never a rendered value") -- this module is where that gets turned into the
text a reader actually sees, with the right NN13 label attached, in exactly
this order of precedence: the latest human edit, then accepted model text,
then the reviewed template fallback the CALLER supplies (a rule finding's
own persisted `findings.observation`/etc., or the deterministic exec-summary
paragraphs below).

This module makes no LLM calls, does no persistence writes, and reads no
run_id-scoped state itself -- every caller (the `act`/`finalise` nodes today;
UI and export code in a later WP) builds its own `narratives_by_target` (via
`persistence.get_narratives(run_id)`, keyed `{(target_kind, target_id,
field): row}`, the same shape `orchestrator.nodes.fieldwork.act` and
`orchestrator.service._narrative_table` already use) and its own placeholder
`table`, and hands both in. That keeps this module usable from a node
(`NodeContext`), a service function (`AppContext`) or an export script with
no persistence access at all (a caller that already has the rows in hand),
without importing any of those.
"""

from __future__ import annotations

import json

from orchestrator.errors import NarrativeVersionMismatch
from orchestrator.narration.placeholders import PlaceholderEntry, class_for_unit, render

__all__ = [
    "LABEL_LLM_UNAVAILABLE",
    "LABEL_MODEL_TEXT_INVALID",
    "accepted_candidate_label",
    "effective_prose",
    "effective_remediation",
    "metrics_placeholder_table",
    "deterministic_exec_summary_paragraphs",
]

# §6.4's exact label strings -- callers must never re-word these.
LABEL_LLM_UNAVAILABLE = "LLM unavailable — deterministic output only"
LABEL_MODEL_TEXT_INVALID = "Deterministic output — model text failed validation"

# The two fallback origins narratives.origin's own CHECK constraint allows
# (migration 011) map onto §6.4's two non-candidate labels; any other value
# reaching this branch (there is none today) gets the more conservative
# "unavailable" label rather than an unlabelled blank.
_FALLBACK_LABELS: dict[str, str] = {
    "fallback_unavailable": LABEL_LLM_UNAVAILABLE,
    "fallback_invalid": LABEL_MODEL_TEXT_INVALID,
}

# origins whose `template_text` is real, renderable prose -- everything else
# (fallback_invalid/fallback_unavailable, or no row at all) has none stored
# (§6.1's own DDL comment: "null for fallbacks").
_TEXT_ORIGINS = frozenset({"model", "model_repaired", "human_edit"})


def accepted_candidate_label(decided_by: str) -> str:
    """§6.4's third label, "an accepted candidate": "AI-proposed, accepted
    by <decided_by>" -- a caller passes this as `accepted_label` when the
    target it is resolving is a `finding_candidates` row (or its copy in
    `findings`, origin='ai_proposed') that has been accepted, so it overrides
    the origin-based label even though the underlying narrative's own origin
    is ordinarily 'model'/'model_repaired' (or 'human_edit', once a
    post-acceptance edit exists)."""
    return f"AI-proposed, accepted by {decided_by}"


def effective_prose(
    *,
    target_kind: str,
    target_id: str,
    field: str,
    narratives_by_target: dict[tuple, dict],
    table: dict[str, PlaceholderEntry],
    fallback_text,
    is_list: bool = False,
    versions: dict[str, int] | None = None,
    accepted_label: str | None = None,
) -> dict:
    """Returns `{text, status, label, sources}` (§6.4):

    * `text` -- the rendered string (or list of strings, for a `field` that
      is list-shaped), never a raw `{class:name}` placeholder.
    * `status` -- the narrative's `origin` ('model', 'model_repaired',
      'human_edit', 'fallback_invalid', 'fallback_unavailable') when a
      rendered narrative exists, else 'fallback_unavailable' (§6.4's own
      table folds "narration disabled" -- no row at all -- into the same
      label as a run-time unavailable call: both mean "show the reviewed
      fallback").
    * `label` -- one of the two exact NN13 strings, `accepted_label`, or
      `None` (nothing to show -- ordinary reviewed model/human text).
    * `sources` -- this narrative's own `sources` (NN12 provenance), `[]`
      for a fallback (nothing was generated to cite).

    `versions`, when given, is `signoff.narration.narrative_versions`
    (§5.2) -- the CURRENT stored row is asserted to already be that exact
    version (edits and regeneration are both blocked once a run leaves
    `awaiting_signoff`, so it always should be); a mismatch raises
    `NarrativeVersionMismatch` rather than silently rendering a version
    nobody signed off on."""
    row = narratives_by_target.get((target_kind, target_id, field))
    if row is None:
        return {"text": fallback_text, "status": "fallback_unavailable", "label": LABEL_LLM_UNAVAILABLE, "sources": []}

    if versions is not None:
        expected = versions.get(row["narrative_id"])
        if expected is not None and expected != row["version"]:
            raise NarrativeVersionMismatch(row["narrative_id"], expected, row["version"])

    origin = row.get("origin")
    if origin in _TEXT_ORIGINS:
        template_text = row.get("template_text")
        if template_text is None:  # defensive only (CLAUDE.md NN14) -- see module docstring
            return {"text": fallback_text, "status": "fallback_unavailable", "label": LABEL_LLM_UNAVAILABLE, "sources": []}
        if is_list:
            text = [render(item, table) for item in json.loads(template_text)]
        else:
            text = render(template_text, table)
        return {"text": text, "status": origin, "label": accepted_label, "sources": row.get("sources") or []}

    label = _FALLBACK_LABELS.get(origin, LABEL_LLM_UNAVAILABLE)
    status = origin if origin in _FALLBACK_LABELS else "fallback_unavailable"
    return {"text": fallback_text, "status": status, "label": label, "sources": []}


def metrics_placeholder_table(names, metrics: dict[str, dict]) -> dict[str, PlaceholderEntry]:
    """The `{name: PlaceholderEntry}` table for a target whose citations are
    a flat list of `run_metrics` names -- a `finding_candidates` row's own
    `metrics_cited` (§3.2's candidate table shape), never a rule finding's
    richer `metrics_cited` dict (that one is `orchestrator.narration.
    payloads.build_finding_table`'s job, unchanged by this WP). Mirrors
    `orchestrator.service._narrative_table`'s own private helper of the same
    shape (WP N9) -- kept here, not imported from there, so this module has
    no dependency on `service.py` (§13: this WP does not touch it)."""
    table: dict[str, PlaceholderEntry] = {}
    for name in names:
        row = metrics.get(name)
        if row is None or row.get("value") is None:
            continue
        unit = row.get("unit")
        table[name] = PlaceholderEntry(
            name=name, unit=unit, value=row["value"], source_field=f"run_metrics.{name}",
            meaning=f"{class_for_unit(unit)} metric, unit {unit}",
        )
    return table


def effective_remediation(
    finding: dict, narratives_by_target: dict[tuple, dict], *, table: dict[str, PlaceholderEntry],
) -> dict:
    """`act`'s own resolution (§2: "An action's description is the
    EFFECTIVE remediation draft, not the raw recommendation"), replacing WP
    N7's narrow `orchestrator.narration.runner.effective_remediation_text`
    stand-in: falls back to this finding's own persisted `recommendation`
    (narration off, or this finding's remediation draft never validated) --
    the SAME fallback `narrate_remediation` itself falls back to when there
    is nothing else to show."""
    finding_id = finding.get("finding_id")
    return effective_prose(
        target_kind="finding", target_id=finding_id or "", field="remediation",
        narratives_by_target=narratives_by_target, table=table, fallback_text=finding.get("recommendation"),
    )


def _money0(value) -> str:
    """Whole-dollar KPI-style rounding -- mirrors
    `orchestrator.pptx_export._money0` (the "Potential exposure" KPI tile).
    Duplicated rather than imported: `orchestrator/pptx_export.py` is WP
    N11's file to change (§4.7's export rebuild, gated on the `dataviz`
    skill), and this module must not depend on it. N11 should retarget
    `pptx_export._build_exec_summary`'s own fallback text to call
    `deterministic_exec_summary_paragraphs` below instead of recomputing it,
    so the two copies do not drift."""
    return f"${value:,.0f}" if isinstance(value, (int, float)) else "—"


def deterministic_exec_summary_paragraphs(
    state, findings: list[dict], metrics: dict[str, dict], n_tests: int,
) -> list[str]:
    """§6.4: "For the exec summary fallback, produce today's deterministic
    paragraphs ... through the resolver, so there is one source." Reproduces
    `orchestrator.pptx_export._build_exec_summary`'s own three inline
    paragraphs verbatim (word for word) so a caller using this function and
    a caller still reading that one (until N11 retargets it) render
    identical text. `findings` is the run's ACCEPTED finding set (rule
    findings, plus any accepted AI-proposed ones once `finalise` has run) --
    the same set the caller's own severity counts should already be over."""
    n_high = sum(1 for f in findings if f.get("severity") == "High")
    n_med = sum(1 for f in findings if f.get("severity") == "Medium")
    n_low = sum(1 for f in findings if f.get("severity") == "Low")
    headline_metric = metrics.get("run_exposure_headline")
    headline_value = headline_metric["value"] if headline_metric else None

    if findings:
        p1 = (
            f"This run assessed {n_tests} deterministic test(s) over the "
            f"{state.audit_period[0]} to {state.audit_period[1]} audit period and raised "
            f"{len(findings)} finding(s): {n_high} High, {n_med} Medium, {n_low} Low."
        )
    else:
        p1 = (
            f"This run assessed the {state.audit_period[0]} to {state.audit_period[1]} audit "
            f"period and raised no findings — every deterministic test passed or was not testable."
        )
    p2 = (
        f"Potential exposure — the amount at risk across every distinct flagged transaction line, "
        f"counted once — is {_money0(headline_value)}. This figure never sums individual findings' "
        f"own cited exposure amounts, which by design overlap."
    )
    p3 = (
        "Every number in this deck comes from this run's own persisted results — none is "
        "recomputed by the export step. See Methodology & limitations for reconciliation, "
        "threshold provenance and not-testable tests."
    )
    return [p1, p2, p3]
