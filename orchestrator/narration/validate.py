"""The narration validator (P6 narration design §3.3-§3.4): the enforcement
half of CLAUDE.md non-negotiable 2 for run-time LLM prose. A model may write
placeholders, identifiers and prose; every literal number, currency symbol,
number word, vague-magnitude claim, universal quantifier, causation/policy
assertion and code-like fragment is rejected here, before the text is ever
stored or shown to an auditor (§3.6 point 1: "Only validated text is
stored"). This module never calls a model and never decides a finding,
number or severity -- it only accepts or rejects text Python was given.

Human-edited narrative gets one deliberate exception (CLAUDE.md §14 Answers
Q3, P6_narration_design.md, amended: "every number an auditor types must
equal a figure the finding cites, as shown"): a typed literal number is
allowed, but must equal -- as the same displayed value, formatting variants
aside -- the rendering of a metric this item cites (`validate_human_edit` /
`origin="human_edit"`). A leading `$` and thousands separators are optional
on a non-percent number; a percent number must still carry `%` and must be
numerically equal to the cited metric's value at its displayed precision.
Every other rule -- placeholder validity, vague language, causation/policy
language, code-like text, length caps -- still applies unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Iterable, Mapping, Sequence

from orchestrator.findings import format_metric_value
from orchestrator.narration.lexicon import (
    FIELD_LENGTH_CAPS,
    OBSERVATION_TYPE_FIELDS,
    RATIONALE_FIELD,
    SEVERITY_CLAIM_FIELDS,
    TITLE_FIELDS,
    find_causal_connectives,
    find_causation_language,
    find_code_like_text,
    find_count_noun_after,
    find_currency_symbols,
    find_exposure_superlative_claims,
    find_hedge_markers,
    find_number_words,
    find_ordinal_words,
    find_percent_words,
    find_policy_assertions,
    find_severity_superlative_claims,
    find_universal_quantifiers,
    find_vague_magnitude_words,
)
from orchestrator.narration.placeholders import (
    PLACEHOLDER_CLASSES,
    NarrationConfigError,
    PlaceholderEntry,
    PlaceholderSpan,
    class_for_unit,
    render,
    scan_placeholders,
    strip_placeholder_spans,
)

__all__ = [
    "NARRATION_VALIDATOR_VERSION",
    "Violation",
    "ValidationResult",
    "validate_prose",
    "validate_human_edit",
    "validate_id_set",
    "validate_themes",
    "required_exec_summary_placeholders",
]

# §3.4: "This is deterministic and documented in the validator constant
# NARRATION_VALIDATOR_VERSION". A change to any rule in this module -- a
# lexicon addition, a coverage change, the sentence-split regex -- must bump
# this, because it enters the run fingerprint's `prompt_template_version`
# hash input (via the node that owns the fingerprint, outside N1's scope).
NARRATION_VALIDATOR_VERSION = "4"

# §3.4, the paragraph under the rule table: sentence boundaries for the
# same-sentence exception in N-Q2, computed after placeholders have been
# rendered to their values.
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

_ORIGINS = ("model", "human_edit")

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# A typed number in a human edit (§14 Q3): an optional leading '$', digits
# with optional thousands commas, an optional decimal part, an optional
# trailing '%'. Bounded so it never matches digits embedded in an
# identifier like "T4.1" or "T6.1d" (excluded by the surrounding
# alnum/dot lookaround, the same boundary the model-prose digit ban uses).
_NUMBER_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_.])\$?\d[\d,]*(?:\.\d+)?%?(?![A-Za-z0-9_])"
)
# The leading `(?<![\d.])` is load-bearing: without it, "42.0%" contains the
# substring "0%" (the trailing digit of "42.0" immediately before the "%"),
# which `\b0%` matches even though the rendered percentage is 42.0, not 0.
# The lookbehind refuses to start a match on a digit that is itself preceded
# by another digit or a decimal point, so only a genuine 0/100-valued
# percentage (e.g. "0%", "0.0%", "100%", "100.00%") can match.
_PCT_ZERO_OR_100_RE = re.compile(r"(?<![\d.])(?:0(?:\.0+)?|100(?:\.0+)?)%")


@dataclass(frozen=True)
class Violation:
    """One rule violation. `text`/`start`/`end` locate the offending
    substring in the text that was scanned when there is a single one to
    point at; a batch-level violation (N-C1, N-X1) leaves them `None`."""

    rule_id: str
    message: str
    text: str | None = None
    start: int | None = None
    end: int | None = None


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    violations: tuple[Violation, ...] = dataclass_field(default_factory=tuple)
    used_placeholders: frozenset[str] = dataclass_field(default_factory=frozenset)

    def rule_ids(self) -> frozenset[str]:
        return frozenset(v.rule_id for v in self.violations)


def _render_for_lexicon(
    text: str, spans: list[PlaceholderSpan], table: Mapping[str, PlaceholderEntry]
) -> str:
    """A best-effort rendering used ONLY for the N-Q2 same-sentence check
    (§3.4's "Sentences are split ... after placeholders have been replaced
    by their rendered values"). Never raises and never used for anything an
    auditor or export reads -- `placeholders.render()` is the one renderer
    that produces text an auditor sees, and it is only ever called on text
    that already passed this validator."""
    out: list[str] = []
    last = 0
    for span in spans:
        out.append(text[last : span.start])
        last = span.end
        rendered = None
        if span.valid_syntax and span.cls in PLACEHOLDER_CLASSES:
            entry = table.get(span.name)
            if entry is not None and entry.value is not None:
                if not (span.cls == "money" and entry.unit != "AUD"):
                    rendered = format_metric_value(entry.value, entry.unit)
        out.append(rendered if rendered is not None else " ")
    out.append(text[last:])
    return "".join(out)


def _check_placeholder_spans(
    text: str,
    spans: list[PlaceholderSpan],
    table: Mapping[str, PlaceholderEntry],
) -> tuple[list[Violation], set[str]]:
    violations: list[Violation] = []
    used: set[str] = set()
    for span in spans:
        if not span.valid_syntax:
            violations.append(
                Violation("N-G1", f"malformed placeholder syntax {span.raw!r}", span.raw, span.start, span.end)
            )
            continue
        if span.cls not in PLACEHOLDER_CLASSES:
            violations.append(
                Violation(
                    "N-G2",
                    f"unknown placeholder class {span.cls!r} in {span.raw!r}",
                    span.raw,
                    span.start,
                    span.end,
                )
            )
            continue
        entry = table.get(span.name)
        if entry is None:
            violations.append(
                Violation(
                    "N-G3",
                    f"placeholder {span.raw!r} is not in this item's table",
                    span.raw,
                    span.start,
                    span.end,
                )
            )
            continue
        used.add(span.name)
        if entry.value is None:
            violations.append(
                Violation("N-V1", f"placeholder {span.raw!r} has a null value", span.raw, span.start, span.end)
            )
        elif class_for_unit(entry.unit) != span.cls:
            violations.append(
                Violation(
                    "N-U1",
                    f"placeholder {span.raw!r} is declared {span.cls!r} but its metric's unit "
                    f"{entry.unit!r} is class {class_for_unit(entry.unit)!r}",
                    span.raw,
                    span.start,
                    span.end,
                )
            )
        if span.cls in ("money", "pct"):
            after = text[span.end :]
            if span.cls == "pct" and re.match(r"^\s*of\b", after, re.IGNORECASE):
                pass  # "pct followed by of is fine" (§3.4 N-U2)
            else:
                hit = find_count_noun_after(after)
                if hit is not None:
                    violations.append(
                        Violation(
                            "N-U2",
                            f"{span.raw!r} is immediately followed by the count noun {hit.text!r}; "
                            "a money/pct placeholder must not be followed by a count noun",
                            hit.text,
                            span.end + hit.start,
                            span.end + hit.end,
                        )
                    )
    return violations, used


def _check_model_digits(text: str, allowed_identifiers: frozenset[str]) -> list[Violation]:
    """§3.3 N-D1, model prose only: no numeral survives outside the
    per-run identifier allow-list (test/control/risk ids, whole-token
    column names), and no currency symbol or "per cent"/"percent" word.
    `text` must already have every placeholder span stripped out, so a
    metric NAME containing a digit (a legitimate placeholder name) is never
    mistaken for model-written prose."""
    violations: list[Violation] = []
    for m in _TOKEN_RE.finditer(text):
        token = m.group(0).rstrip(".")
        if not token or not any(ch.isnumeric() for ch in token):
            continue
        if token in allowed_identifiers:
            continue
        violations.append(
            Violation(
                "N-D1",
                f"numeral {token!r} is not allowed in model prose; use a placeholder instead",
                token,
                m.start(),
                m.start() + len(token),
            )
        )
    for hit in find_currency_symbols(text):
        violations.append(
            Violation(
                "N-D1",
                f"currency symbol {hit.text!r} is not allowed in model prose; use a money placeholder instead",
                hit.text,
                hit.start,
                hit.end,
            )
        )
    for hit in find_percent_words(text):
        violations.append(
            Violation(
                "N-D1",
                f"{hit.text!r} is not allowed in model prose; use a pct placeholder instead",
                hit.text,
                hit.start,
                hit.end,
            )
        )
    return violations


def _human_edit_lookup(table: Mapping[str, PlaceholderEntry]) -> tuple[set[str], set[float]]:
    """Builds the two structures `_human_edit_number_matches` compares a
    typed token against (the user's "every number an auditor types must
    equal a figure the finding cites, as shown" decision, CLAUDE.md §14
    Answers Q3, treating formatting variants of the SAME displayed value as
    equal rather than requiring a byte-identical string):

    - `plain_values`: every non-percent metric's rendering
      (`format_metric_value`) with its optional leading `$` and thousands
      commas stripped, so "1234" and "1,234" are both accepted for a
      rendered "1,234", and "1234.56"/"1,234.56"/"$1,234.56" are all
      accepted for a rendered "$1,234.56". These are compared as strings:
      money always renders to a fixed two decimal places, so there is no
      precision ambiguity to resolve numerically.
    - `pct_values`: the numeric value of every percent metric. A percent
      comparison is numeric, not string, because `format_metric_value`'s
      percent rendering is Python's own `str(value)` and so varies with
      whether the underlying value is an int or a float ("15%" vs "15.0%")
      without the underlying quantity differing at all. Comparing the typed
      number and the metric's value as numbers -- both at full, unrounded
      precision -- accepts exactly the formatting variants of one value and
      nothing else: "15%" matches a rendered "15.0%" (equal), but does not
      match a rendered "15.3%" (a different value, not a formatting
      variant) and typing "15.30%" instead of "15.3%" still requires the
      values themselves to be equal, not merely a truncated prefix.
    """
    plain_values: set[str] = set()
    pct_values: set[float] = set()
    for entry in table.values():
        if entry.value is None:
            continue
        if entry.unit == "%":
            try:
                pct_values.add(float(entry.value))
            except (TypeError, ValueError):
                continue
        else:
            rendered = format_metric_value(entry.value, entry.unit)
            plain_values.add(rendered.lstrip("$").replace(",", ""))
    return plain_values, pct_values


def _human_edit_number_matches(token: str, plain_values: set[str], pct_values: set[float]) -> bool:
    """§14 Answers Q3 (amended): "Percent must still carry '%'" -- a typed
    token is only ever compared against `pct_values` when it itself ends in
    `%`, and never falls back to `plain_values` in that case, so a typed
    number that drops the `%` a percent metric requires is refused even
    when the digits are right."""
    if token.endswith("%"):
        numeric_part = token[:-1].lstrip("$").replace(",", "")
        try:
            value = float(numeric_part)
        except ValueError:
            return False
        return value in pct_values
    normalized = token.lstrip("$").replace(",", "")
    return normalized in plain_values


def _check_human_edit_numbers(
    text: str, table: Mapping[str, PlaceholderEntry], allowed_identifiers: frozenset[str] = frozenset(),
) -> list[Violation]:
    """§14 Answers Q3: a human edit may type a literal number, but it must
    equal -- as the SAME displayed value, formatting variants aside
    (`_human_edit_lookup`/`_human_edit_number_matches`) -- the rendering
    `orchestrator.findings.format_metric_value` produces for some metric
    this item cites. A mismatch names the exact substring the auditor typed
    (a structured `Violation`, rule id N-H1), so the edit UI can point at
    what needs fixing rather than just refusing silently.

    BUG-P2-2 (independent review 2026-09-26): N-D1 (model origin) exempts a
    digit that is part of an ALLOWED IDENTIFIER (a cited finding's own
    test/control/risk id, e.g. "CTL-TNE-13") by tokenising with `_TOKEN_RE`,
    which treats a hyphen as part of the token -- "CTL-TNE-13" is one token,
    matched whole against `allowed_identifiers`. `_NUMBER_TOKEN_RE`'s own
    boundary does NOT exclude a hyphen, so "13" inside that SAME identifier
    reads as a bare, unmatched typed number here, even though it was never
    typed as a quantity and was already valid, allowed model prose. Every
    number a human edit ever sees came from ALREADY-VALIDATED text (model
    prose, or a prior edit), so ANY identifier allowed at generation must be
    exempt from N-H1 too -- otherwise editing a field that legitimately
    names a control/risk/test id refuses even a purely additive change that
    never touches that id. Skip a number match that falls entirely inside a
    `_TOKEN_RE` token equal to one of `allowed_identifiers`, the SAME
    identifier source `_check_model_digits` already uses, never a second,
    independently-maintained set (CLAUDE.md's own "one source of truth")."""
    plain_values, pct_values = _human_edit_lookup(table)
    identifier_spans = [
        (m.start(), m.end()) for m in _TOKEN_RE.finditer(text) if m.group(0) in allowed_identifiers
    ]
    violations: list[Violation] = []
    for m in _NUMBER_TOKEN_RE.finditer(text):
        # Containment on the number match's START only, not its end:
        # `_NUMBER_TOKEN_RE`'s own `[\d,]*` greedily swallows a comma right
        # after the digits even when it is ordinary sentence punctuation
        # ("CTL-TNE-13, 8 bookings" matches "13," -- the comma is not part
        # of the id token `_TOKEN_RE` found), so the number match can run
        # one character past where the identifier ends. The digit run
        # itself still BEGINS inside the identifier, which is what matters.
        if any(start <= m.start() < end for start, end in identifier_spans):
            continue
        token = m.group(0)
        if _human_edit_number_matches(token, plain_values, pct_values):
            continue
        violations.append(
            Violation(
                "N-H1",
                f"typed number {token!r} does not match the displayed rendering of any metric this item cites",
                token,
                m.start(),
                m.end(),
            )
        )
    return violations


def validate_prose(
    text: str,
    table: Mapping[str, PlaceholderEntry],
    *,
    field: str,
    origin: str = "model",
    allowed_identifiers: Iterable[str] = (),
    require_coverage: bool = False,
    required_placeholders: Iterable[str] | None = None,
) -> ValidationResult:
    """Validates one piece of prose against §3.3-§3.4's rules.

    `field` selects the field-scoped rules: the N-L1 length cap, whether
    N-L2 (no placeholders in a title), N-D3 (no ordinals -- `rationale`
    only) and N-Q1/N-Q2 (vague/universal-quantifier language --
    `OBSERVATION_TYPE_FIELDS` only) apply.

    `origin="model"` (default) enforces the full literal-number ban (N-D1).
    `origin="human_edit"` (§14 Answers Q3) replaces N-D1 with N-H1: a typed
    number is allowed if and only if it equals a rendered metric value this
    item's table carries. Every other rule applies under both origins.

    `require_coverage`/`required_placeholders` implement N-C1: when set,
    every name in `required_placeholders` (or, if not given, every
    non-null entry in `table`) must be referenced by a valid placeholder
    somewhere in `text`.
    """
    if origin not in _ORIGINS:
        raise ValueError(f"unknown origin {origin!r}; expected one of {_ORIGINS}")

    violations: list[Violation] = []
    allowed_ids = frozenset(allowed_identifiers)

    spans = scan_placeholders(text)

    if field in TITLE_FIELDS and spans:
        first = spans[0]
        violations.append(
            Violation("N-L2", f"{field!r} must not contain placeholders", first.raw, first.start, first.end)
        )

    span_violations, used_placeholders = _check_placeholder_spans(text, spans, table)
    violations.extend(span_violations)

    # N-L1 (quality review 2026-09-25): measures what an auditor actually
    # reads -- the RENDERED text -- not the model's typed placeholder
    # source. `{pct:att_missing_pct}` alone is 21 raw characters for a
    # value that renders to e.g. "12.3%", 5 -- measuring the unrendered
    # form systematically starved the real prose budget on
    # placeholder-heavy fields (found live: every `priority rationale` in
    # two full runs failed this cap, both before and after its one repair,
    # because 2-3 typed placeholders alone can eat half of it). This never
    # loosens what a placeholder may say (§3.3-3.4's other rules, N-G3,
    # N-D1 etc. are unchanged and still run on the raw/stripped text below)
    # -- it only changes which string N-L1's own len() is measured against,
    # using the SAME render() a valid item's text is stored/shown with.
    # When a span is itself invalid, `_check_placeholder_spans` above
    # already raised its own violation for it (and `render()` would raise
    # trying to resolve it) -- the raw text is measured instead, exactly
    # the OLD behaviour, only for that already-flagged case.
    cap = FIELD_LENGTH_CAPS.get(field)
    if cap is not None:
        if span_violations:
            length_text = text
        else:
            try:
                length_text = render(text, table)
            except NarrationConfigError:
                length_text = text
        if len(length_text) > cap:
            violations.append(
                Violation("N-L1", f"{field!r} is {len(length_text)} characters (rendered), over the {cap}-character cap")
            )

    stripped = strip_placeholder_spans(text, spans)

    if origin == "model":
        violations.extend(_check_model_digits(stripped, allowed_ids))
    else:
        violations.extend(_check_human_edit_numbers(stripped, table, allowed_ids))

    for hit in find_number_words(stripped):
        violations.append(
            Violation(
                "N-D2",
                f"number word {hit.text!r} is not allowed; use a placeholder instead",
                hit.text,
                hit.start,
                hit.end,
            )
        )

    if field == RATIONALE_FIELD:
        for hit in find_ordinal_words(stripped):
            violations.append(
                Violation(
                    "N-D3",
                    f"ranking word {hit.text!r} is not allowed in priority rationale; "
                    "Python owns the ordering",
                    hit.text,
                    hit.start,
                    hit.end,
                )
            )

    if field in OBSERVATION_TYPE_FIELDS:
        for hit in find_vague_magnitude_words(stripped):
            violations.append(
                Violation(
                    "N-Q1",
                    f"vague magnitude word {hit.text!r} is not allowed; let the placeholder carry the size",
                    hit.text,
                    hit.start,
                    hit.end,
                )
            )

        rendered_for_lexicon = _render_for_lexicon(text, spans, table)
        for sentence in SENTENCE_SPLIT_RE.split(rendered_for_lexicon):
            for hit in find_universal_quantifiers(sentence):
                if not _PCT_ZERO_OR_100_RE.search(sentence):
                    violations.append(
                        Violation(
                            "N-Q2",
                            f"universal quantifier {hit.text!r} needs a 0%% or 100%% placeholder "
                            "in the same sentence",
                            hit.text,
                        )
                    )

        # N-S5 (independent narration-content review 2026-09-25, round 3):
        # a superlative exposure/amount claim ("the highest exposure",
        # "the primary contributor to the amount at risk", ...) is only
        # ever true of the payload's OWN declared largest-exposure finding
        # (`run_exposure_dominant_title`, `orchestrator.narration.
        # run_values._dominant_exposure_entries`) -- `top_findings[0]` is
        # ordered by severity, tie-broken by exposure, which is a different
        # ranking and is NOT guaranteed to be the same finding. Checked at
        # the whole-TEXT level (one exec-summary paragraph, one caption, ...)
        # rather than per-sentence: legitimate audit prose routinely
        # disambiguates in a following sentence of the SAME paragraph
        # ("The primary contributor ... is the high-value claims finding
        # ... This figure ... specifically the finding titled High-Value
        # Claims Requiring Enhanced Scrutiny." -- both sentences are one
        # paragraph, one `validate_prose` call). Only runs when this item's
        # own table carries `run_exposure_dominant_title` with a real
        # value -- a finding-scoped field (`observation`, `recommendation`,
        # ...) never merges that run-level placeholder into its own table,
        # so this is a no-op there, not a false negative.
        dominant_entry = table.get("run_exposure_dominant_title")
        dominant_title = dominant_entry.value if dominant_entry is not None else None
        if dominant_title:
            if str(dominant_title).casefold() not in rendered_for_lexicon.casefold():
                for hit in find_exposure_superlative_claims(rendered_for_lexicon):
                    violations.append(
                        Violation(
                            "N-S5",
                            f"{hit.text!r} claims a finding is ranked first by exposure/amount, but "
                            f"this text never names {dominant_title!r} -- the payload's own "
                            "largest-exposure finding (run_exposure_dominant_title); only that "
                            "finding may be described with an exposure/amount superlative",
                            hit.text,
                        )
                    )

        # N-S6 (independent narration-content review 2026-09-25, round 6):
        # a severity-superlative claim ("top-ranked severity", "highest
        # severity", "most severe/serious", "the primary/main/key/top
        # finding", "high-severity") is only ever true of a finding this
        # run's OWN rules actually scored High -- never merely the finding
        # named first, and never the run's largest-EXPOSURE contributor
        # unless that finding is ALSO High (`run_values.
        # _severity_high_entries`'s `run_high_finding_<n>_title` entries are
        # the ground truth; see that function's docstring for the two live
        # failures this closes). Scoped to `SEVERITY_CLAIM_FIELDS` only --
        # NOT every `OBSERVATION_TYPE_FIELDS` member -- because a chart
        # caption legitimately calls the run's own dominant-EXPOSURE finding
        # "the top finding ... contributing $X" (an N-S5 case, not this
        # one); see that frozenset's own comment. Only runs when this item's
        # own table carries at least one `run_high_finding_*_title` entry
        # with a real value -- a run with no High-severity finding, or a
        # finding-scoped table that never merges the run-level table in the
        # first place, is a no-op here, not a false negative (mirrors N-S5's
        # own gating above).
        if field in SEVERITY_CLAIM_FIELDS:
            high_titles = {
                str(entry.value).casefold()
                for name, entry in table.items()
                if name.startswith("run_high_finding_") and name.endswith("_title") and entry.value
            }
            if high_titles:
                low = rendered_for_lexicon.casefold()
                if not any(title in low for title in high_titles):
                    for hit in find_severity_superlative_claims(rendered_for_lexicon):
                        violations.append(
                            Violation(
                                "N-S6",
                                f"{hit.text!r} claims a finding is highest-severity/most-severe/"
                                "primary, but this text never names any of this run's actual "
                                "High-severity findings (run_high_finding_*_title) -- only a "
                                "finding this run's rules scored High may be described with a "
                                "severity superlative",
                                hit.text,
                            )
                        )

    for hit in find_causation_language(stripped):
        violations.append(
            Violation(
                "N-S1",
                f"{hit.text!r} implies intent or causation, which is never a fact here",
                hit.text,
                hit.start,
                hit.end,
            )
        )

    for hit in find_policy_assertions(stripped):
        violations.append(
            Violation(
                "N-S2",
                f"{hit.text!r} asserts a policy; thresholds are analyst-set, not policy (CLAUDE.md §0.4)",
                hit.text,
                hit.start,
                hit.end,
            )
        )

    # G12-lite / N-S4 (independent narration-content review 2026-09-25; see
    # `lexicon.find_causal_connectives`'s own docstring): a causal
    # connective ("result(s/ed) in", "thereby", "caused"/"causes",
    # "lead(s)/led to") asserts a definite cause-and-effect UNLESS a hedge
    # marker ("may", "could", "potential(ly)", "risk of", "indicates a risk
    # of", ...) appears anywhere in the SAME sentence -- checked per
    # sentence, not per whole field, so a hedge earlier in a long paragraph
    # cannot license an unhedged claim several sentences later, and a hedge
    # later in the same sentence still licenses an earlier connective
    # ("Duplicate claims may result in ... loss" and "result in ... loss,
    # which may indicate ..." both pass; a bare "result in ... loss" with no
    # hedge anywhere in that sentence does not). Runs on `stripped` (already
    # placeholder-free, like N-S1/N-S2 above) -- no rendering is needed
    # since every word involved is literal, never a placeholder's value.
    # An interrogative sentence (ends "?") is exempt: "What causes a claim
    # to be missing its register match?" and "...a process that doesn't
    # result in an expense report?" (both real, pre-existing management
    # questions -- tests/narration_test_support.py, skills/tne_exco/
    # findings.yaml T3.1a) ASK about a cause, they do not ASSERT one; only
    # a declarative sentence can assert a definite cause-and-effect, which
    # is the thing this rule exists to catch.
    for sentence in SENTENCE_SPLIT_RE.split(stripped):
        if sentence.rstrip().endswith("?"):
            continue
        connective_hits = find_causal_connectives(sentence)
        if not connective_hits:
            continue
        if find_hedge_markers(sentence):
            continue
        for hit in connective_hits:
            violations.append(
                Violation(
                    "N-S4",
                    f"{hit.text!r} asserts a definite cause or result with no hedge word (\"may\", "
                    "\"could\", \"potential\", \"risk of\") anywhere in the same sentence; a cause "
                    "is a hypothesis, never a fact",
                    hit.text,
                )
            )

    for hit in find_code_like_text(text):
        violations.append(
            Violation("N-S3", f"{hit.text!r} looks like code, not audit prose", hit.text, hit.start, hit.end)
        )

    if require_coverage:
        if required_placeholders is not None:
            required = frozenset(required_placeholders)
        else:
            required = frozenset(name for name, entry in table.items() if entry.value is not None)
        missing = sorted(required - used_placeholders)
        if missing:
            violations.append(Violation("N-C1", f"missing required placeholder(s): {missing}"))

    return ValidationResult(valid=not violations, violations=tuple(violations), used_placeholders=frozenset(used_placeholders))


def required_exec_summary_placeholders(table: Mapping[str, PlaceholderEntry]) -> frozenset[str]:
    """N-C1, `exec_summary` only (round-5 narration-content review, item 2;
    extended round 6, item 2): a live round-4 exec summary correctly cited
    `run_finding_count` and `run_exposure_headline` but never said WHICH
    finding drives the headline or how much of it -- it omitted
    `run_exposure_dominant_title`/`run_exposure_dominant_amount` even though
    the payload declared them
    (`orchestrator.narration.run_values._dominant_exposure_entries`) and
    `exec_summary_user.md` explicitly asks the model to name them -- and
    still passed, because `narrate_exec_summary`'s own coverage check never
    looked at those two names. A live round-5 exec summary repeated the
    same shape of gap one level up: it named `run_finding_count` and
    `run_exposure_headline` but never named EITHER of the run's own
    High-severity findings (`run_values._severity_high_entries`'s
    `run_high_finding_<n>_title` entries) even though both existed with
    real values -- so this now requires every such entry too, one per
    actual High-severity finding, not merely the first. Same gating style
    as N-S5 throughout: a name is required only when THIS item's own table
    actually declares it with a real value (`entry.value is not None`) -- a
    clean run, a run with no High-severity finding, or a run whose exposure
    has no single dominant contributor, requires nothing extra, and a
    finding-scoped table (which never carries these run-level names at all,
    per `build_finding_table`) is unaffected. `run_finding_count` is always
    required, matching the pre-existing behaviour this extends."""
    required = {"run_finding_count"}
    headline = table.get("run_exposure_headline")
    if headline is not None and headline.value is not None:
        required.add("run_exposure_headline")
    dominant_amount = table.get("run_exposure_dominant_amount")
    if dominant_amount is not None and dominant_amount.value is not None:
        required.add("run_exposure_dominant_amount")
        required.add("run_exposure_dominant_title")
    approved_not_spent = table.get("run_approved_not_spent_total")
    if approved_not_spent is not None and approved_not_spent.value is not None:
        required.add("run_approved_not_spent_total")
    for name, entry in table.items():
        if name.startswith("run_high_finding_") and name.endswith("_title") and entry.value is not None:
            required.add(name)
    return frozenset(required)


def validate_human_edit(
    text: str,
    table: Mapping[str, PlaceholderEntry],
    *,
    field: str,
    allowed_identifiers: Iterable[str] = (),
    require_coverage: bool = False,
    required_placeholders: Iterable[str] | None = None,
) -> ValidationResult:
    """Convenience wrapper: `validate_prose(..., origin="human_edit")`
    (§14 Answers Q3). See that docstring and `_check_human_edit_numbers`
    for what changes relative to model output. `allowed_identifiers` (BUG-
    P2-2) must be the SAME set generation validated this item's prose
    against (`orchestrator.service._narrative_allowed_identifiers`) -- an
    empty default is the pre-existing, narrower behaviour for a caller with
    no identifiers to offer, never a silent "everything allowed"."""
    return validate_prose(
        text,
        table,
        field=field,
        origin="human_edit",
        allowed_identifiers=allowed_identifiers,
        require_coverage=require_coverage,
        required_placeholders=required_placeholders,
    )


def validate_id_set(produced_ids: Sequence[str], expected_ids: Sequence[str]) -> list[Violation]:
    """§3.4 N-X1 (structural, part 1): every produced id is in the expected
    enum, no id is duplicated, and every expected id has exactly one item."""
    violations: list[Violation] = []
    expected = set(expected_ids)
    seen: set[str] = set()
    duplicates: set[str] = set()
    unknown: set[str] = set()
    for pid in produced_ids:
        if pid not in expected:
            unknown.add(pid)
        elif pid in seen:
            duplicates.add(pid)
        else:
            seen.add(pid)
    if unknown:
        violations.append(Violation("N-X1", f"id(s) not in the expected enum: {sorted(unknown)}"))
    if duplicates:
        violations.append(Violation("N-X1", f"duplicate id(s): {sorted(duplicates)}"))
    missing = sorted(expected - seen)
    if missing:
        violations.append(Violation("N-X1", f"missing expected id(s): {missing}"))
    return violations


def validate_themes(
    themes: Sequence[Mapping[str, Any]],
    *,
    valid_finding_keys: Iterable[str],
    max_themes: int = 6,
    max_review_observations: int = 3,
) -> list[Violation]:
    """§3.4 N-X1 (structural, part 2 -- synthesis output): at most
    `max_themes` themes; every finding key a theme names is a real finding
    for this run; each finding key belongs to at most one theme; at most
    `max_review_observations` review observations per theme."""
    violations: list[Violation] = []
    if len(themes) > max_themes:
        violations.append(Violation("N-X1", f"{len(themes)} themes exceeds the cap of {max_themes}"))

    valid_keys = set(valid_finding_keys)
    membership_count: dict[str, int] = {}
    for i, theme in enumerate(themes):
        finding_keys = list(theme.get("finding_keys", []))
        unknown = [k for k in finding_keys if k not in valid_keys]
        if unknown:
            violations.append(Violation("N-X1", f"theme {i}: unknown finding key(s): {unknown}"))
        for k in finding_keys:
            membership_count[k] = membership_count.get(k, 0) + 1

        review_observations = list(theme.get("review_observations", []))
        if len(review_observations) > max_review_observations:
            violations.append(
                Violation(
                    "N-X1",
                    f"theme {i}: {len(review_observations)} review observations exceeds the cap of "
                    f"{max_review_observations}",
                )
            )

    multi_theme = sorted(k for k, count in membership_count.items() if count > 1)
    if multi_theme:
        violations.append(Violation("N-X1", f"finding key(s) appear in more than one theme: {multi_theme}"))

    return violations
