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
    TITLE_FIELDS,
    find_causation_language,
    find_code_like_text,
    find_count_noun_after,
    find_currency_symbols,
    find_number_words,
    find_ordinal_words,
    find_percent_words,
    find_policy_assertions,
    find_universal_quantifiers,
    find_vague_magnitude_words,
)
from orchestrator.narration.placeholders import (
    PLACEHOLDER_CLASSES,
    PlaceholderEntry,
    PlaceholderSpan,
    class_for_unit,
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
]

# §3.4: "This is deterministic and documented in the validator constant
# NARRATION_VALIDATOR_VERSION". A change to any rule in this module -- a
# lexicon addition, a coverage change, the sentence-split regex -- must bump
# this, because it enters the run fingerprint's `prompt_template_version`
# hash input (via the node that owns the fingerprint, outside N1's scope).
NARRATION_VALIDATOR_VERSION = "1"

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
_PCT_ZERO_OR_100_RE = re.compile(r"\b(?:0(?:\.0+)?|100(?:\.0+)?)%")


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


def _check_human_edit_numbers(text: str, table: Mapping[str, PlaceholderEntry]) -> list[Violation]:
    """§14 Answers Q3: a human edit may type a literal number, but it must
    equal -- as the SAME displayed value, formatting variants aside
    (`_human_edit_lookup`/`_human_edit_number_matches`) -- the rendering
    `orchestrator.findings.format_metric_value` produces for some metric
    this item cites. A mismatch names the exact substring the auditor typed
    (a structured `Violation`, rule id N-H1), so the edit UI can point at
    what needs fixing rather than just refusing silently."""
    plain_values, pct_values = _human_edit_lookup(table)
    violations: list[Violation] = []
    for m in _NUMBER_TOKEN_RE.finditer(text):
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

    cap = FIELD_LENGTH_CAPS.get(field)
    if cap is not None and len(text) > cap:
        violations.append(
            Violation("N-L1", f"{field!r} is {len(text)} characters, over the {cap}-character cap")
        )

    spans = scan_placeholders(text)

    if field in TITLE_FIELDS and spans:
        first = spans[0]
        violations.append(
            Violation("N-L2", f"{field!r} must not contain placeholders", first.raw, first.start, first.end)
        )

    span_violations, used_placeholders = _check_placeholder_spans(text, spans, table)
    violations.extend(span_violations)

    stripped = strip_placeholder_spans(text, spans)

    if origin == "model":
        violations.extend(_check_model_digits(stripped, allowed_ids))
    else:
        violations.extend(_check_human_edit_numbers(stripped, table))

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


def validate_human_edit(
    text: str,
    table: Mapping[str, PlaceholderEntry],
    *,
    field: str,
    require_coverage: bool = False,
    required_placeholders: Iterable[str] | None = None,
) -> ValidationResult:
    """Convenience wrapper: `validate_prose(..., origin="human_edit")`
    (§14 Answers Q3). See that docstring and `_check_human_edit_numbers`
    for what changes relative to model output."""
    return validate_prose(
        text,
        table,
        field=field,
        origin="human_edit",
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
