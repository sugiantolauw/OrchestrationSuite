"""Typed-placeholder grammar and renderer (P6 narration design §3.1).

CLAUDE.md non-negotiable 2: the runtime LLM never invents a number. It writes
`{class:name}` placeholders; Python is the only thing that turns one into a
formatted figure, using the SAME formatter `findings._format_template` already
uses for `findings.yaml`'s bare `{name}` templates
(`orchestrator.findings.format_metric_value`) -- never a second formatter.

Parsing is a hand-written scanner over brace-delimited groups, not
`str.format`/`string.Formatter`: those would silently accept format specs
(`{count:x:,.0f}`), conversions (`{x!r}`), attribute/index access (`{a.b}`,
`{a[0]}`) and `{{`-escaping, none of which this grammar allows. Every one of
those must fail loudly as a grammar violation (N-G1 in
`orchestrator.narration.validate`), never be silently accepted or silently
dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from orchestrator.findings import format_metric_value

__all__ = [
    "NarrationConfigError",
    "PLACEHOLDER_CLASSES",
    "PlaceholderEntry",
    "PlaceholderSpan",
    "class_for_unit",
    "scan_placeholders",
    "strip_placeholder_spans",
    "render",
]


class NarrationConfigError(Exception):
    """Raised when a placeholder cannot be rendered: an unknown name, a null
    value, or -- CLAUDE.md NN14 -- a `money`-class placeholder whose metric's
    unit is not AUD, for which `format_metric_value` has no formatter and
    would otherwise silently fall through to a raw, currency-less `str(value)`.
    Never call `render()` on unvalidated model output; `validate.py` must
    reject these cases first (N-G3, N-V1, N-U1) so this is only ever raised on
    a genuine implementation bug, not on live model output."""


# §3.1: the closed set of placeholder classes a model may write.
PLACEHOLDER_CLASSES: tuple[str, ...] = ("count", "pct", "money", "duration", "ratio", "date", "value")

_DURATION_UNITS = frozenset({"days", "minutes", "hours"})
_ISO_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

# The `name` grammar: `[a-z][a-z0-9_]{0,63}`.
_NAME_RE = r"[a-z][a-z0-9_]{0,63}"
# The generic shape check is deliberately class-agnostic (any lowercase
# word before the colon) -- it separates "not `class:name`-shaped at all"
# (N-G1: `{{x}}`, `{x!r}`, `{a.b}`, `{count:x:,.0f}`) from "shaped like a
# placeholder but the class word isn't one of the seven" (N-G2), which
# `validate.py` reports as two distinct rule ids.
_PLACEHOLDER_CONTENT_RE = re.compile(rf"^(?P<cls>[a-z][a-z0-9_]*):(?P<name>{_NAME_RE})$")
# Matches the shallowest brace-delimited group -- no nested braces inside.
# Anything the grammar does not consume as a valid inner match (an outer
# `{` in `{{x}}`, a second unmatched `}`, ...) is left in the text for the
# caller to notice as a stray brace (also N-G1).
_BRACE_GROUP_RE = re.compile(r"\{([^{}]*)\}")


def class_for_unit(unit: str) -> str:
    """§3.1's "class <- unit" table: the placeholder class a metric's own
    unit renders as. This is a property of the metric, independent of
    whatever class a model happened to write -- `validate.py` compares the
    two (N-U1)."""
    if unit == "count":
        return "count"
    if unit == "%":
        return "pct"
    if unit == "AUD" or (unit and _ISO_CURRENCY_RE.match(unit)):
        return "money"
    if unit in _DURATION_UNITS:
        return "duration"
    if unit == "ratio":
        return "ratio"
    if unit == "date":
        return "date"
    return "value"


@dataclass(frozen=True)
class PlaceholderSpan:
    """One `{...}` group found in model or human-edit text.

    `valid_syntax` is purely about the `class:name` SHAPE (§3.1 grammar):
    `cls`/`name` are only populated when the inner text matches
    `<word>:<name>`. Whether `cls` is one of the seven known classes
    (N-G2) and whether `name` is a placeholder this item's table actually
    has (N-G3) are validator-level concerns, not scanner-level ones.
    """

    start: int
    end: int
    raw: str
    valid_syntax: bool
    cls: str | None = None
    name: str | None = None


def scan_placeholders(text: str) -> list[PlaceholderSpan]:
    """Finds every brace-delimited group in `text`, valid or not.

    Returning invalid spans too (rather than silently skipping them) is
    deliberate: `validate.py` needs them to raise N-G1, and
    `strip_placeholder_spans` needs them to remove every `{...}` group --
    including a malformed one -- before the literal-digit/number-word
    lexicon (§3.3) runs, so stray placeholder syntax is never mistaken for
    model-written prose.
    """
    spans: list[PlaceholderSpan] = []
    for m in _BRACE_GROUP_RE.finditer(text):
        inner = m.group(1)
        content_match = _PLACEHOLDER_CONTENT_RE.match(inner)
        if content_match:
            spans.append(
                PlaceholderSpan(
                    start=m.start(),
                    end=m.end(),
                    raw=m.group(0),
                    valid_syntax=True,
                    cls=content_match.group("cls"),
                    name=content_match.group("name"),
                )
            )
        else:
            spans.append(PlaceholderSpan(start=m.start(), end=m.end(), raw=m.group(0), valid_syntax=False))
    return spans


def strip_placeholder_spans(text: str, spans: list[PlaceholderSpan] | None = None) -> str:
    """Removes every `{...}` group (valid or not) and any leftover brace
    character not consumed as part of one, leaving prose with no
    placeholder syntax at all -- what §3.3's "checked after placeholder
    spans are removed" literal-digit and number-word lexicon runs against.

    The leftover-brace pass matters for `{{x}}`: the scanner above matches
    only the inner `{x}` (braces do not nest), so without this pass the
    outer `{`/`}` pair would still be sitting in the "stripped" text.
    """
    if spans is None:
        spans = scan_placeholders(text)
    out: list[str] = []
    last = 0
    for span in spans:
        out.append(text[last : span.start])
        # A single space in place of the whole removed span -- not a plain
        # delete -- keeps token boundaries intact on either side (e.g.
        # "a{x}b" -> "a b", not "ab") so the lexicon's word-boundary regexes
        # see two separate words rather than an accidental merge.
        out.append(" ")
        last = span.end
    out.append(text[last:])
    stripped = "".join(out)
    # Any brace character not consumed as part of a matched span (the outer
    # pair in "{{x}}", a stray unmatched "}") is replaced the same way.
    return stripped.replace("{", " ").replace("}", " ")


@dataclass(frozen=True)
class PlaceholderEntry:
    """One row of an item's placeholder table (§3.2): a metric this item may
    cite, keyed by `name` in the table passed to `render`/`validate_prose`.

    `value=None` marks a metric that exists for this run but could not be
    computed (e.g. a `not_testable` test) -- §3.2: "Rows whose value is None
    are omitted" from what the model is shown, but the row is still carried
    here so a model reference to it is caught as N-V1 (a known metric with no
    value) rather than N-G3 (an unknown name entirely).

    `source_field` and `meaning` are NN12 provenance/display metadata
    (`"run_metrics.missing_receipt_count"`, a human-readable description);
    building them from a run's actual metrics is the payload builder's job
    (P6 WP N5), not this module's -- this dataclass only fixes the shape.
    """

    name: str
    unit: str
    value: Any
    source_field: str | None = None
    meaning: str | None = None

    @property
    def cls(self) -> str:
        return class_for_unit(self.unit)


def render(text: str, table: Mapping[str, PlaceholderEntry]) -> str:
    """Renders validated prose: every `{class:name}` span becomes
    `orchestrator.findings.format_metric_value(entry.value, entry.unit)` --
    exactly what `_format_template` produces for a findings.yaml template's
    bare `{name}` form today. Never a second formatter (§1 #1 of the design).

    Only call this on text that has already passed
    `validate.validate_prose`/`validate_human_edit`: a malformed span, an
    unknown name, a null value, or a non-AUD `money` placeholder all raise
    `NarrationConfigError` here rather than being silently rendered, because
    reaching this function with any of those means the validator was
    skipped, not that the model produced acceptable output.
    """
    spans = scan_placeholders(text)
    out: list[str] = []
    last = 0
    for span in spans:
        out.append(text[last : span.start])
        last = span.end
        if not span.valid_syntax:
            raise NarrationConfigError(f"cannot render malformed placeholder {span.raw!r}")
        entry = table.get(span.name)
        if entry is None:
            raise NarrationConfigError(f"placeholder {span.raw!r} is not in this item's table")
        if entry.value is None:
            raise NarrationConfigError(f"placeholder {span.raw!r} has no value to render")
        if span.cls == "money" and entry.unit != "AUD":
            raise NarrationConfigError(
                f"placeholder {span.raw!r}: no formatter for money unit {entry.unit!r} (only AUD is supported)"
            )
        out.append(format_metric_value(entry.value, entry.unit))
    out.append(text[last:])
    return "".join(out)
