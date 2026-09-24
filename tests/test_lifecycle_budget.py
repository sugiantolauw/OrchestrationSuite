"""L0.3 (LIFECYCLE_design.md §2.6): per-run Ceiling/BudgetMeter/
BudgetedGateway and monthly admission. Two things this suite must prove:
every ceiling actually triggers (never a silent no-op check that would let
a run exceed a limit nobody enforced), and usage is derived from the
`llm_calls` ledger rather than held in memory -- so a resumed run (a fresh
BudgetMeter/BudgetedGateway instance built against the same persistence and
run_id, exactly what a new executor pass after an App restart would do)
continues the same budget rather than resetting it."""

from __future__ import annotations

import pytest

from orchestrator.config import Settings
from orchestrator.errors import CeilingReached, ConfigError, MonthlyLLMBudgetExceeded
from orchestrator.lifecycle.budget import BudgetedGateway, BudgetMeter, Ceiling, check_monthly_admission
from orchestrator.llm.gateway import CallContext
from tests.conftest import canonical_ts
from tests.test_persistence_contract import _llm_call_row


def _ceiling(**overrides):
    base = dict(max_llm_calls=3, max_total_tokens=1_000, max_wall_s=60)
    base.update(overrides)
    return Ceiling(**base)


# ── BudgetMeter.usage / check ────────────────────────────────────────────────


def test_usage_is_zero_for_a_run_with_no_llm_calls(persistence, uid):
    meter = BudgetMeter(persistence=persistence, run_id=f"RUN-{uid}", ceiling=_ceiling())
    assert meter.usage() == {"llm_calls": 0, "total_tokens": 0}


def test_usage_reflects_ledger_rows_for_this_run_id_only(persistence, uid):
    run_id = f"RUN-USAGE-{uid}"
    other_run_id = f"RUN-USAGE-OTHER-{uid}"
    persistence.record_llm_call(_llm_call_row(f"CALL-A-{uid}", run_id=run_id, total_tokens=100))
    persistence.record_llm_call(_llm_call_row(f"CALL-B-{uid}", run_id=run_id, total_tokens=50))
    persistence.record_llm_call(_llm_call_row(f"CALL-OTHER-{uid}", run_id=other_run_id, total_tokens=999))

    meter = BudgetMeter(persistence=persistence, run_id=run_id, ceiling=_ceiling())
    assert meter.usage() == {"llm_calls": 2, "total_tokens": 150}


def test_check_llm_calls_passes_under_the_ceiling(persistence, uid):
    run_id = f"RUN-CALLS-OK-{uid}"
    persistence.record_llm_call(_llm_call_row(f"CALL-{uid}", run_id=run_id))
    meter = BudgetMeter(persistence=persistence, run_id=run_id, ceiling=_ceiling(max_llm_calls=3))
    meter.check("llm_calls")  # 1 existing + this call = 2 <= 3 -- must not raise


def test_check_llm_calls_raises_ceiling_reached_at_the_limit(persistence, uid):
    run_id = f"RUN-CALLS-CEIL-{uid}"
    persistence.record_llm_call(_llm_call_row(f"CALL-A-{uid}", run_id=run_id))
    persistence.record_llm_call(_llm_call_row(f"CALL-B-{uid}", run_id=run_id))
    persistence.record_llm_call(_llm_call_row(f"CALL-C-{uid}", run_id=run_id))
    meter = BudgetMeter(persistence=persistence, run_id=run_id, ceiling=_ceiling(max_llm_calls=3))
    with pytest.raises(CeilingReached) as exc:
        meter.check("llm_calls")
    assert exc.value.which == "max_llm_calls"
    assert exc.value.used == 3
    assert exc.value.limit == 3


def test_check_total_tokens_passes_under_the_ceiling(persistence, uid):
    run_id = f"RUN-TOK-OK-{uid}"
    persistence.record_llm_call(_llm_call_row(f"CALL-{uid}", run_id=run_id, total_tokens=100))
    meter = BudgetMeter(persistence=persistence, run_id=run_id, ceiling=_ceiling(max_total_tokens=1_000))
    meter.check("total_tokens", estimated_tokens=500)  # 100 + 500 <= 1000


def test_check_total_tokens_raises_ceiling_reached_over_the_limit(persistence, uid):
    run_id = f"RUN-TOK-CEIL-{uid}"
    persistence.record_llm_call(_llm_call_row(f"CALL-{uid}", run_id=run_id, total_tokens=900))
    meter = BudgetMeter(persistence=persistence, run_id=run_id, ceiling=_ceiling(max_total_tokens=1_000))
    with pytest.raises(CeilingReached) as exc:
        meter.check("total_tokens", estimated_tokens=200)  # 900 + 200 > 1000
    assert exc.value.which == "max_total_tokens"
    assert exc.value.used == 900
    assert exc.value.limit == 1_000


def test_check_unsupported_ceiling_kind_raises_config_error_not_a_silent_pass(persistence, uid):
    # max_docs_read/max_queries/max_rounds have no ledger yet (L1/L2 add
    # knowledge_reads/research_queries) -- checking one of them today must
    # fail loudly, never silently let a run through unbudgeted.
    meter = BudgetMeter(persistence=persistence, run_id=f"RUN-{uid}", ceiling=_ceiling(max_docs_read=10))
    with pytest.raises(ConfigError):
        meter.check("docs_read")


def test_a_resumed_run_continues_the_same_budget_across_a_fresh_meter_instance(persistence, uid):
    # Simulates a container restart: the first BudgetMeter instance (this
    # executor pass) is discarded, and a brand-new one is built against the
    # same persistence/run_id (the next executor pass) -- usage must reflect
    # everything recorded so far, not reset to zero.
    run_id = f"RUN-RESUME-{uid}"
    ceiling = _ceiling(max_llm_calls=2)
    meter_pass_1 = BudgetMeter(persistence=persistence, run_id=run_id, ceiling=ceiling)
    meter_pass_1.check("llm_calls")  # 0 existing -- fine
    persistence.record_llm_call(_llm_call_row(f"CALL-A-{uid}", run_id=run_id))
    persistence.record_llm_call(_llm_call_row(f"CALL-B-{uid}", run_id=run_id))

    meter_pass_2 = BudgetMeter(persistence=persistence, run_id=run_id, ceiling=ceiling)
    assert meter_pass_2.usage() == {"llm_calls": 2, "total_tokens": 4}
    with pytest.raises(CeilingReached):
        meter_pass_2.check("llm_calls")


# ── BudgetedGateway ───────────────────────────────────────────────────────────


class _FakeGateway:
    def __init__(self):
        self.calls: list[dict] = []

    def call(self, *, task, seq, messages, desired_params, schema=None, ctx):
        self.calls.append({"task": task, "seq": seq, "messages": messages})
        return "the-real-gateway-was-called"


def test_budgeted_gateway_delegates_to_the_wrapped_gateway_when_under_ceiling(persistence, uid):
    run_id = f"RUN-BG-OK-{uid}"
    fake = _FakeGateway()
    gw = BudgetedGateway(fake, persistence=persistence, run_id=run_id, ceiling=_ceiling())
    result = gw.call(
        task="sensing_plan", seq=1, messages=[{"role": "user", "content": "hello"}],
        desired_params={}, ctx=CallContext(run_id=run_id),
    )
    assert result == "the-real-gateway-was-called"
    assert len(fake.calls) == 1


def test_budgeted_gateway_raises_ceiling_reached_without_calling_the_wrapped_gateway(persistence, uid):
    run_id = f"RUN-BG-CEIL-{uid}"
    persistence.record_llm_call(_llm_call_row(f"CALL-{uid}", run_id=run_id))
    fake = _FakeGateway()
    gw = BudgetedGateway(fake, persistence=persistence, run_id=run_id, ceiling=_ceiling(max_llm_calls=1))
    with pytest.raises(CeilingReached):
        gw.call(
            task="sensing_plan", seq=2, messages=[{"role": "user", "content": "hello"}],
            desired_params={}, ctx=CallContext(run_id=run_id),
        )
    assert fake.calls == []  # the wrapped gateway must never see this call


def test_budgeted_gateway_checks_total_tokens_from_prompt_length_and_max_tokens(persistence, uid):
    run_id = f"RUN-BG-TOK-{uid}"
    fake = _FakeGateway()
    # 3000-char prompt -> ~1000 estimated prompt tokens (len/3), plus a
    # requested max_tokens of 500 -- 1500 estimated > the 1000 ceiling.
    long_message = [{"role": "user", "content": "x" * 3_000}]
    gw = BudgetedGateway(fake, persistence=persistence, run_id=run_id, ceiling=_ceiling(max_total_tokens=1_000))
    with pytest.raises(CeilingReached) as exc:
        gw.call(task="sensing_plan", seq=1, messages=long_message, desired_params={"max_tokens": 500}, ctx=CallContext(run_id=run_id))
    assert exc.value.which == "max_total_tokens"
    assert fake.calls == []


# ── check_monthly_admission ──────────────────────────────────────────────────


def test_monthly_admission_requires_the_budget_setting(persistence):
    settings = Settings()  # llm_monthly_token_budget defaults to None
    with pytest.raises(ConfigError):
        check_monthly_admission(
            persistence=persistence, settings=settings, ceiling=_ceiling(), now_iso=canonical_ts(0),
        )


def test_monthly_admission_passes_when_within_budget(persistence, uid):
    settings = Settings(llm_monthly_token_budget=10_000)
    persistence.record_llm_call(_llm_call_row(f"CALL-{uid}", run_id=f"RUN-{uid}", total_tokens=100, created_at="2026-05-10T00:00:00.000000Z"))
    check_monthly_admission(
        persistence=persistence, settings=settings, ceiling=_ceiling(max_total_tokens=500),
        now_iso="2026-05-20T00:00:00.000000Z",
    )  # must not raise -- well within budget for a fresh month


def test_monthly_admission_raises_when_projected_usage_would_exceed_budget(persistence, uid):
    settings = Settings(llm_monthly_token_budget=1_000)
    persistence.record_llm_call(_llm_call_row(
        f"CALL-{uid}", run_id=f"RUN-{uid}", total_tokens=900, created_at="2026-06-05T00:00:00.000000Z",
    ))
    with pytest.raises(MonthlyLLMBudgetExceeded) as exc:
        check_monthly_admission(
            persistence=persistence, settings=settings, ceiling=_ceiling(max_total_tokens=500),
            now_iso="2026-06-20T00:00:00.000000Z",
        )
    assert exc.value.budget == 1_000
    assert exc.value.requested_tokens == 500


def test_monthly_admission_excludes_prior_months_usage(persistence, uid):
    settings = Settings(llm_monthly_token_budget=1_000)
    # All of this run's usage is in a PRIOR month -- a naive "sum everything
    # ever" implementation would refuse; the real one, scoped to the current
    # UTC month, must not.
    persistence.record_llm_call(_llm_call_row(
        f"CALL-{uid}", run_id=f"RUN-{uid}", total_tokens=900, created_at="2026-06-30T23:59:59.999999Z",
    ))
    check_monthly_admission(
        persistence=persistence, settings=settings, ceiling=_ceiling(max_total_tokens=500),
        now_iso="2026-07-01T00:00:00.000000Z",
    )  # July has no usage of its own yet -- must not raise
