"""Per-run ceilings and monthly admission for lifecycle run_kinds that call a
model (LIFECYCLE_design.md §2.6). This package is never imported by the
fieldwork pipeline (LI-5, tests/test_lifecycle_isolation.py) -- `execute`
never calls an LLM at all (CLAUDE.md non-negotiable 2).

Two independent things live here:

  - `Ceiling` / `BudgetMeter` / `BudgetedGateway`: a hard, per-run cap on how
    much a single lifecycle run may spend (calls, tokens, wall time, and --
    once orchestrator.research/orchestrator.knowledge exist -- documents
    read, queries issued, research rounds). Usage is DERIVED from the
    existing ledger (`llm_calls` for the dimensions this package can already
    check), never held only in memory, so a resumed run continues the same
    budget rather than resetting it by restarting.
  - `check_monthly_admission`: a coarser, cross-run cap checked once, at the
    moment a lifecycle run that calls a model is created -- never polled.
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator.errors import CeilingReached, ConfigError, MonthlyLLMBudgetExceeded
from orchestrator.timeutil import is_canonical_ts


@dataclass(frozen=True)
class Ceiling:
    max_llm_calls: int
    max_total_tokens: int
    max_wall_s: int
    max_docs_read: int | None = None
    max_queries: int | None = None
    max_rounds: int | None = None


# The two ceiling dimensions BudgetMeter can check today, since only
# `llm_calls` is a ledger this package (and the tables L0.1 created) can
# already derive usage from. `max_docs_read`/`max_queries`/`max_rounds` are
# declared on Ceiling now (LIFECYCLE_design.md §2.6's own shape) so a future
# lifecycle kind's Ceiling instance can already state them; BudgetMeter.check
# raises a clear ConfigError for any of those kinds until L1/L2 add the
# knowledge_reads/research_queries ledgers to derive usage from -- never a
# silent no-op check that would let a run exceed a limit nobody enforced.
_LLM_CALL_LEDGER_KINDS = ("llm_calls", "total_tokens")


class BudgetMeter:
    """Usage is DERIVED from the ledger (`llm_calls` for this run_id), never
    held only in memory -- a resumed run continues the same budget; restart
    cannot reset it. `check(kind, estimated_tokens)` raises
    CeilingReached(which, used, limit) when admitting the call now would
    exceed `kind`'s ceiling.

    `kind` names which ceiling dimension to check: "llm_calls" (a plain
    count, `estimated_tokens` is ignored) or "total_tokens" (checks
    `used + estimated_tokens` against `ceiling.max_total_tokens`).
    `BudgetedGateway.call` checks both, in that order, before every logical
    call -- this mirrors the spec text ("checks max_llm_calls and ... <=
    max_total_tokens") as two calls to the same method rather than folding
    both dimensions into one call whose meaning would be harder to log and
    to test in isolation."""

    def __init__(self, *, persistence, run_id: str, ceiling: Ceiling):
        self._persistence = persistence
        self._run_id = run_id
        self._ceiling = ceiling

    def usage(self) -> dict[str, int]:
        rows = self._persistence.list_llm_calls(self._run_id)
        return {
            "llm_calls": len(rows),
            "total_tokens": sum(r.get("total_tokens") or 0 for r in rows),
        }

    def check(self, kind: str, estimated_tokens: int = 0) -> None:
        if kind not in _LLM_CALL_LEDGER_KINDS:
            raise ConfigError(
                f"BudgetMeter.check: no ledger to derive usage for ceiling kind {kind!r} yet"
            )
        used = self.usage()
        if kind == "llm_calls":
            projected = used["llm_calls"] + 1
            if projected > self._ceiling.max_llm_calls:
                raise CeilingReached("max_llm_calls", used["llm_calls"], self._ceiling.max_llm_calls)
        else:
            projected = used["total_tokens"] + estimated_tokens
            if projected > self._ceiling.max_total_tokens:
                raise CeilingReached("max_total_tokens", used["total_tokens"], self._ceiling.max_total_tokens)


def _estimate_prompt_tokens(messages: list[dict]) -> int:
    # LIFECYCLE_design.md §2.6: "The estimate is len(prompt_chars) / 3."
    # Conservative and intentionally crude -- the ledger's own real
    # total_tokens (recorded by the wrapped LLMGateway after the call
    # returns) is what actually counts toward the NEXT check, never this
    # estimate.
    chars = sum(len(m.get("content") or "") for m in messages)
    return chars // 3


class BudgetedGateway:
    """Wraps `orchestrator.llm.gateway.LLMGateway` (never modifies it) with a
    per-run `Ceiling`. Before every logical call, checks `max_llm_calls` and
    `used_tokens + estimated_prompt_tokens + max_tokens <= max_total_tokens`
    via `BudgetMeter`. A `CeilingReached` raised here is not this class's
    concern to interpret -- the calling node catches it, records
    `module_output.stop_reason = f"ceiling:{exc.which}"` and marks its own
    coverage incomplete, then finishes; there is never a silent partial
    result (CLAUDE.md §9A item 7)."""

    def __init__(self, gateway, *, persistence, run_id: str, ceiling: Ceiling):
        self._gateway = gateway
        self._meter = BudgetMeter(persistence=persistence, run_id=run_id, ceiling=ceiling)

    def call(self, *, task: str, seq: int, messages: list[dict], desired_params: dict, schema: dict | None = None, ctx):
        self._meter.check("llm_calls")
        estimated = _estimate_prompt_tokens(messages) + int(desired_params.get("max_tokens") or 0)
        self._meter.check("total_tokens", estimated)
        return self._gateway.call(
            task=task, seq=seq, messages=messages, desired_params=desired_params, schema=schema, ctx=ctx,
        )


def _month_start(now_iso: str) -> str:
    if not is_canonical_ts(now_iso):
        raise ValueError(f"not a canonical timestamp: {now_iso!r}")
    return now_iso[:7] + "-01T00:00:00.000000Z"


def check_monthly_admission(*, persistence, settings, ceiling: Ceiling, now_iso: str) -> None:
    """LIFECYCLE_design.md §2.6: LLM_MONTHLY_TOKEN_BUDGET is REQUIRED to
    start any lifecycle run_kind that calls a model -- fieldwork never calls
    this (CLAUDE.md non-negotiable 2: `execute` never calls an LLM). Sums
    llm_calls.total_tokens for the current UTC month in one query (never
    polled) and refuses, with a reason, if that sum plus this run_kind's own
    Ceiling.max_total_tokens would exceed the configured budget."""
    budget = settings.llm_monthly_token_budget
    if budget is None:
        raise ConfigError(
            "LLM_MONTHLY_TOKEN_BUDGET is required to start a lifecycle run_kind that calls a model"
        )
    used = persistence.sum_llm_call_tokens_since(_month_start(now_iso))
    if used + ceiling.max_total_tokens > budget:
        raise MonthlyLLMBudgetExceeded(
            used_tokens=used, requested_tokens=ceiling.max_total_tokens, budget=budget,
        )
