"""T4.3 row-level LLM classification (independent review 2026-09-24 item 4).
CLAUDE.md §11 corporate-workspace decision: `ai_query()`/`ai_classify()` are
denied on the corporate warehouse, so this is a plain Python batch loop
through orchestrator.llm.gateway.LLMGateway -- never `ai_query()`. Built,
switched off by `Settings.enable_row_level_llm` (env `ENABLE_ROW_LEVEL_LLM`,
default false): while off, T4.3 stays `not_testable` with reason "awaiting
governance approval to send expense descriptions to a model" and nothing in
this module is ever called from the live pipeline.

PII-safe by construction: `classify_rows` receives only the two columns it
needs (a row key and the text to classify) -- never a whole row, never a
whole population. The caller is responsible for projecting down to exactly
those columns before calling in (see `orchestrator.nodes.fieldwork` for the
gated integration point, itself never enabled by this change)."""

from __future__ import annotations

import json
from dataclasses import dataclass

from orchestrator.llm.gateway import CallContext, LLMGateway

CLASSIFY_TASK = "classify"
DEFAULT_BATCH_SIZE = 20

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "row_key": {"type": "string"},
                    "personal_expense": {"type": "boolean"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "rationale": {"type": "string"},
                },
                "required": ["row_key", "personal_expense", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["results"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = (
    "You are reviewing expense claim descriptions for indicators of personal (non-business) "
    "spend, for an internal audit test (T4.3). For each claim below, decide personal_expense "
    "(true if the description suggests personal/non-business spend, false otherwise), a "
    "confidence between 0 and 1, and a short rationale. Return only JSON matching the given "
    "schema -- no prose, no markdown code fences."
)


@dataclass(frozen=True)
class ClassificationResult:
    row_key: str
    personal_expense: bool
    confidence: float
    rationale: str | None
    call_id: str


def classify_rows(
    rows: list[dict],
    *,
    text_column: str,
    row_key_column: str,
    gateway: LLMGateway,
    ctx: CallContext,
    batch_size: int = DEFAULT_BATCH_SIZE,
    seq_start: int = 1,
) -> list[ClassificationResult]:
    """`rows` must already be projected to ONLY `row_key_column` and
    `text_column` by the caller -- this function never sees, and therefore
    cannot leak, any other column (the PII-safety rule this capability
    exists to satisfy). Batches are capped at `batch_size` rows per model
    call, bounding both prompt size and the blast radius of one bad
    response.

    A batch whose call does not succeed (LLM unavailable, or output that
    fails schema validation after its one retry -- both handled inside
    LLMGateway) contributes NO results for its rows: never a fabricated
    confidence (CLAUDE.md NN14). A row present in the response but missing
    from `rows` (a hallucinated row_key) is silently dropped -- only rows
    this call actually asked about can appear in the result. A row in
    `rows` absent from the response is simply not classified this run.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")

    by_key = {r[row_key_column]: r for r in rows}
    results: list[ClassificationResult] = []
    seq = seq_start
    for batch in _batched(rows, batch_size):
        payload = [{"row_key": r[row_key_column], "text": r[text_column]} for r in batch]
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({"claims": payload}, ensure_ascii=False)},
        ]
        result = gateway.call(
            task=CLASSIFY_TASK,
            seq=seq,
            messages=messages,
            desired_params={"max_tokens": 4000, "temperature": 0, "reasoning_effort": "low"},
            schema=CLASSIFY_SCHEMA,
            ctx=ctx,
        )
        seq += 1
        if result.status != "ok" or not result.parsed:
            continue
        batch_keys = {r[row_key_column] for r in batch}
        for item in result.parsed.get("results", []):
            key = item.get("row_key")
            if key not in batch_keys or key not in by_key:
                continue  # never trust a row_key this batch didn't ask about
            results.append(
                ClassificationResult(
                    row_key=key,
                    personal_expense=bool(item["personal_expense"]),
                    confidence=float(item["confidence"]),
                    rationale=item.get("rationale"),
                    call_id=result.call_id,
                )
            )
    return results


def to_persisted_rows(results: list[ClassificationResult], *, now: str) -> list[dict]:
    """The shape orchestrator.adapters.protocols.PersistenceAdapter.
    write_classification_results expects."""
    return [
        {
            "row_key": r.row_key, "personal_expense": r.personal_expense,
            "confidence": r.confidence, "rationale": r.rationale, "call_id": r.call_id,
            "created_at": now,
        }
        for r in results
    ]


def _batched(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]
