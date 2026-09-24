"""apply_plan_edits (docs/specs/P6_P8_explorer_llm_design.md §4.10): a pure
function over an ALREADY-canonical PlanProposal (orchestrator.explorer.
canonical.to_canonical's output -- what the plan node stores at
`RunState.plan["proposal"]`) plus the auditor's accumulated `plan_edits`
(D4: include/exclude per test only, plus the column/enum/threshold ops the
backend supports from day one even though the D4 UI exposes only
include/exclude for now).

Applying an edit never re-runs the planner and never re-validates by
itself -- that is orchestrator.service.edit_explorer_plan's job, which calls
`validate_proposal` on the result and refuses the WHOLE batch if it would
leave any still-included test invalid (§4.10: "nothing partial is ever
recorded"). This module only computes what the proposal WOULD look like."""

from __future__ import annotations

import copy
import re
from typing import Any

_PATH_SEGMENT_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*)(\[(\d+)\])?$")


def parse_param_path(param_path: str) -> list[str | int]:
    """"params.column" -> ["params", "column"]; "params.group_by[0]" ->
    ["params", "group_by", 0]; "params.metrics.total_amount.column" ->
    ["params", "metrics", "total_amount", "column"] (canonical form keeps
    `metrics` as a {name: spec} dict, so "total_amount" is a plain dict key,
    not an index -- the same traversal handles both without distinguishing
    them)."""
    parts: list[str | int] = []
    for segment in param_path.split("."):
        m = _PATH_SEGMENT_RE.match(segment)
        if not m:
            raise ValueError(f"invalid param_path segment: {segment!r}")
        parts.append(m.group(1))
        if m.group(3) is not None:
            parts.append(int(m.group(3)))
    return parts


def _deep_set(root: dict, path: list[str | int], value: Any) -> None:
    node = root
    for part in path[:-1]:
        if isinstance(part, int):
            node = node[part]
        else:
            node = node.setdefault(part, {})
    last = path[-1]
    if isinstance(last, int):
        node[last] = value
    else:
        node[last] = value


def apply_plan_edits(proposal: dict, edits: list[dict]) -> dict:
    """Returns a NEW canonical proposal: every `set_column`/`set_enum`/
    `set_threshold` edit applied in order, then narrowed to exclude every
    test any `exclude_test` (not undone by a later `include_test` for the
    same key) named -- and, cascading, every finding whose `test_key` is no
    longer present. Risks/controls are left as-is; materialise() (§4.11)
    narrows those to only the ones a kept test still references. Unknown
    test/threshold keys in an edit are silently no-ops (the caller,
    service.edit_explorer_plan, only ever offers keys it read from this same
    proposal -- CLAUDE.md NN14 does not apply to a value the auditor never
    typed themselves)."""
    proposal = copy.deepcopy(proposal)
    excluded: set[str] = set()
    tests_by_key = {t["key"]: t for t in proposal.get("tests", [])}
    thresholds_by_id = {th["id"]: th for th in proposal.get("thresholds", [])}

    for edit in edits:
        op = edit.get("op")
        if op == "exclude_test":
            excluded.add(edit["test_key"])
        elif op == "include_test":
            excluded.discard(edit["test_key"])
        elif op in ("set_column", "set_enum"):
            test = tests_by_key.get(edit["test_key"])
            if test is None:
                continue
            _deep_set(test, parse_param_path(edit["param_path"]), edit["value"])
        elif op == "set_threshold":
            th = thresholds_by_id.get(edit["threshold_id"])
            if th is not None:
                th["value"] = edit["value"]
        else:
            raise ValueError(f"apply_plan_edits: unknown edit op {op!r}")

    kept_tests = [t for t in proposal.get("tests", []) if t["key"] not in excluded]
    kept_test_keys = {t["key"] for t in kept_tests}
    kept_findings = [f for f in proposal.get("findings", []) if f.get("test_key") in kept_test_keys]

    proposal["tests"] = kept_tests
    proposal["findings"] = kept_findings
    return proposal


def excluded_test_keys(edits: list[dict]) -> set[str]:
    """The current excluded set implied by `edits` in order -- used by
    service.get_explorer_review to show each test's include/exclude state
    without re-deriving it from apply_plan_edits' full output."""
    excluded: set[str] = set()
    for edit in edits:
        op = edit.get("op")
        if op == "exclude_test":
            excluded.add(edit["test_key"])
        elif op == "include_test":
            excluded.discard(edit["test_key"])
    return excluded
