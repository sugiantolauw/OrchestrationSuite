"""Run-time narration: number-safe placeholders, the prose validator, and
(in later P6 work packages) the payload builders, wire schemas, prompts and
the `narrate`/`finalise` nodes described in `docs/specs/P6_narration_design.md`.

This package makes no LLM calls itself. What is built so far (WP N1, per
that spec's §12 table):

- `orchestrator.narration.placeholders` -- the `{class:name}` grammar and
  the renderer, reusing `orchestrator.findings.format_metric_value`;
- `orchestrator.narration.lexicon` -- the banned-word/phrase lists and
  per-field constants (§3.3-§3.4);
- `orchestrator.narration.validate` -- the validator that turns those into
  pass/fail decisions with structured `Violation`s, for both model output
  and human edits (CLAUDE.md §14 Answers Q3).

Everything else named in the design spec (payload builders, wire schemas,
prompts, `run_values`, candidates, the `narrate`/`finalise` nodes) is a
later work package and does not exist in this package yet.
"""

from __future__ import annotations

from orchestrator.narration.lexicon import (
    FIELD_LENGTH_CAPS,
    OBSERVATION_TYPE_FIELDS,
    RATIONALE_FIELD,
    TITLE_FIELDS,
)
from orchestrator.narration.placeholders import (
    NarrationConfigError,
    PLACEHOLDER_CLASSES,
    PlaceholderEntry,
    PlaceholderSpan,
    class_for_unit,
    render,
    scan_placeholders,
    strip_placeholder_spans,
)
from orchestrator.narration.validate import (
    NARRATION_VALIDATOR_VERSION,
    ValidationResult,
    Violation,
    validate_human_edit,
    validate_id_set,
    validate_prose,
    validate_themes,
)

__all__ = [
    "FIELD_LENGTH_CAPS",
    "NARRATION_VALIDATOR_VERSION",
    "NarrationConfigError",
    "OBSERVATION_TYPE_FIELDS",
    "PLACEHOLDER_CLASSES",
    "PlaceholderEntry",
    "PlaceholderSpan",
    "RATIONALE_FIELD",
    "TITLE_FIELDS",
    "ValidationResult",
    "Violation",
    "class_for_unit",
    "render",
    "scan_placeholders",
    "strip_placeholder_spans",
    "validate_human_edit",
    "validate_id_set",
    "validate_prose",
    "validate_themes",
]
