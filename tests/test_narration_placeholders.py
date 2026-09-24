"""T-P1 (docs/specs/P6_narration_design.md §11, §12 WP N1): the placeholder
grammar accepts and rejects the right inputs, the renderer's output equals
`orchestrator.findings.format_metric_value` (never a second formatter), and
a non-AUD `money` placeholder raises."""

from __future__ import annotations

import pytest

from orchestrator.findings import format_metric_value
from orchestrator.narration.placeholders import (
    NarrationConfigError,
    PlaceholderEntry,
    class_for_unit,
    render,
    scan_placeholders,
    strip_placeholder_spans,
)


# ---------------------------------------------------------------------------
# Grammar: `scan_placeholders` accepts a well-formed span.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,cls,name",
    [
        ("{count:missing_receipt_count}", "count", "missing_receipt_count"),
        ("{money:t6_1d_daily_over_amount}", "money", "t6_1d_daily_over_amount"),
        ("{pct:missing_receipt_pct}", "pct", "missing_receipt_pct"),
        ("{date:run_audit_period_start}", "date", "run_audit_period_start"),
        ("{value:x}", "value", "x"),
        ("{ratio:r1}", "ratio", "r1"),
        ("{duration:days_open}", "duration", "days_open"),
        ("{count:a}", "count", "a"),  # single-character name is legal
    ],
)
def test_scan_placeholders_accepts_valid_spans(text, cls, name):
    spans = scan_placeholders(text)
    assert len(spans) == 1
    span = spans[0]
    assert span.valid_syntax is True
    assert span.cls == cls
    assert span.name == name
    assert span.raw == text


# ---------------------------------------------------------------------------
# Grammar: rejects everything `str.format`/`string.Formatter` would accept
# but this grammar does not -- format specs, conversions, attribute access,
# indexing, `{{` escaping, an unknown class word, and a name that fails its
# own character rules.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "{count:x:,.0f}",  # format spec
        "{x!r}",  # conversion, no class at all
        "{count:a.b}",  # attribute access inside the name
        "{count:a[0]}",  # indexing
        "{count:}",  # empty name
        "{:missing_receipt_count}",  # empty class
        "{count:1abc}",  # name must start with a letter
        "{count:Missing_Receipt_Count}",  # name must be lowercase
        "{count : missing_receipt_count}",  # whitespace inside the span
    ],
)
def test_scan_placeholders_rejects_malformed_spans(text):
    spans = scan_placeholders(text)
    assert len(spans) == 1
    assert spans[0].valid_syntax is False


def test_scan_placeholders_double_brace_escaping_is_all_invalid():
    # `{{x}}`: the scanner (no nesting) only matches the inner "{x}", which
    # itself fails the class:name grammar (no colon) -- and the outer,
    # unmatched brace characters are what strip_placeholder_spans removes
    # next, so nothing here is ever treated as a valid placeholder.
    spans = scan_placeholders("{{x}}")
    assert len(spans) == 1
    assert spans[0].valid_syntax is False
    assert spans[0].raw == "{x}"


def test_scan_placeholders_finds_multiple_spans_independently():
    text = "{count:a} and {money:b} and not-a-placeholder and {pct:c}"
    spans = scan_placeholders(text)
    assert [s.name for s in spans if s.valid_syntax] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# class_for_unit: the §3.1 "class <- unit" table.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "unit,expected_cls",
    [
        ("count", "count"),
        ("%", "pct"),
        ("AUD", "money"),
        ("USD", "money"),
        ("SGD", "money"),
        ("days", "duration"),
        ("minutes", "duration"),
        ("hours", "duration"),
        ("ratio", "ratio"),
        ("date", "date"),
        ("employee_id", "value"),
        ("", "value"),
    ],
)
def test_class_for_unit(unit, expected_cls):
    assert class_for_unit(unit) == expected_cls


# ---------------------------------------------------------------------------
# strip_placeholder_spans: removes every brace-delimited group (valid or
# not) and any leftover unmatched brace character, leaving prose with no
# placeholder syntax and no accidental word-merging.
# ---------------------------------------------------------------------------
def test_strip_placeholder_spans_removes_valid_and_invalid_spans():
    text = "a{count:x}b {{y}} c{count:x:,.0f}d"
    stripped = strip_placeholder_spans(text)
    assert "{" not in stripped
    assert "}" not in stripped
    # word boundaries are preserved -- "a" and "b" never get glued together
    assert "ab" not in stripped


# ---------------------------------------------------------------------------
# render(): must equal orchestrator.findings.format_metric_value exactly --
# this IS the "never write a second formatter" requirement (P6 design §1 #1).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "unit,value",
    [
        ("count", 1234),
        ("count", 0),
        ("AUD", 6000.5),
        ("AUD", 0.0),
        ("%", 8.5),
        ("%", 100.0),
        ("ratio", 1.5),
        ("date", "2025-06-30"),
        ("employee_id", "52725"),
    ],
)
def test_render_matches_format_metric_value_exactly(unit, value):
    cls = class_for_unit(unit)
    if cls == "money" and unit != "AUD":
        pytest.skip("non-AUD money is covered by its own raising test")
    table = {"x": PlaceholderEntry("x", unit, value)}
    text = f"the value is {{{cls}:x}}"
    rendered = render(text, table)
    assert rendered == f"the value is {format_metric_value(value, unit)}"


def test_render_multiple_placeholders_in_one_string():
    table = {
        "missing_receipt_count": PlaceholderEntry("missing_receipt_count", "count", 12),
        "missing_receipt_pct": PlaceholderEntry("missing_receipt_pct", "%", 8.5),
        "missing_receipt_amount": PlaceholderEntry("missing_receipt_amount", "AUD", 6000.0),
    }
    text = (
        "{count:missing_receipt_count} claims ({pct:missing_receipt_pct} of total) are "
        "missing receipts, totalling {money:missing_receipt_amount}."
    )
    assert render(text, table) == (
        f"{format_metric_value(12, 'count')} claims "
        f"({format_metric_value(8.5, '%')} of total) are missing receipts, "
        f"totalling {format_metric_value(6000.0, 'AUD')}."
    )


def test_render_non_aud_money_raises_narration_config_error():
    table = {"x": PlaceholderEntry("x", "USD", 100.0)}
    with pytest.raises(NarrationConfigError):
        render("{money:x}", table)


def test_render_unknown_placeholder_raises():
    with pytest.raises(NarrationConfigError):
        render("{count:not_in_table}", {})


def test_render_null_value_raises():
    table = {"x": PlaceholderEntry("x", "count", None)}
    with pytest.raises(NarrationConfigError):
        render("{count:x}", table)


def test_render_malformed_span_raises():
    with pytest.raises(NarrationConfigError):
        render("{count:x:,.0f}", {"x": PlaceholderEntry("x", "count", 1)})


def test_placeholder_entry_cls_property_matches_class_for_unit():
    entry = PlaceholderEntry("x", "AUD", 100.0)
    assert entry.cls == class_for_unit("AUD") == "money"
