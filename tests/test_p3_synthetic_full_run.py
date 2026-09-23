"""The one full run against the REAL synthetic_data/ (CLAUDE.md build brief P3
§5's "full local run through service.start_audit_run with ORCH_BACKEND=local
on synthetic_data/"): start_audit_run -> awaiting_signoff -> sign_off ->
completed with an XLSX export, over the real 8 SKILL-001 source files rather
than a fixture. This is genuinely slow -- xlsx parsing the ~30MB
Expense_Report_Combined.xlsx costs roughly 50s per full read, and the run
reads bound sources several times across discover/profile/execute/
prioritise/G6 -- so it is kept in its own file, skipped when synthetic_data/
is absent, and not part of the fast suite this session runs on every save."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from orchestrator import service

REPO_ROOT = Path(__file__).parent.parent
SYNTHETIC_DATA_DIR = REPO_ROOT / "synthetic_data"

pytestmark = pytest.mark.skipif(
    not SYNTHETIC_DATA_DIR.is_dir(), reason="synthetic_data/ not present in this checkout"
)


def _wait_for_status(ctx, run_id, statuses, timeout):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = service.get_run(ctx, run_id)["status"]
        if last in statuses:
            return last, time.time()
        time.sleep(1)
    raise AssertionError(f"run {run_id} did not reach {statuses} in {timeout}s (last={last})")


def test_full_run_against_real_synthetic_data(tmp_path):
    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(SYNTHETIC_DATA_DIR),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
        "ORCH_WORKER_ID": "synthetic-full-run",
    }
    ctx = service.build_app_context(env)
    ctx.executor.start()
    started = time.time()
    try:
        bindings = service.suggest_bindings(ctx, "SKILL-001")
        assert all(bindings.values()), bindings

        run_id = service.start_audit_run(
            ctx, skill_id="SKILL-001", bindings=bindings,
            audit_period=("2025-01-01", "2026-04-30"),
            objective="Full local run against real synthetic_data/", run_owner="tester",
        )

        status, reached_at = _wait_for_status(ctx, run_id, {"awaiting_signoff", "failed"}, timeout=600)
        run = service.get_run(ctx, run_id)
        assert status == "awaiting_signoff", run.get("status_reason")
        duration_s = reached_at - started
        print(f"\nsynthetic_data/ full run reached awaiting_signoff in {duration_s:.1f}s")

        payload = service.get_run_payload(ctx, run_id)
        assert payload["reconciliation"]
        for source, rec in payload["reconciliation"].items():
            assert rec["variance"] == 0, f"{source}: {rec}"  # G6, real data

        # CLAUDE.md P2/P3 gate review item B1: every monetary finding's
        # exposure_amount must be consistent with the amount metric(s) it
        # cites -- the regression this guards against (RUN-5C6A997EC940,
        # T6.1d daily-spend exceedances) is that exposure_amount was computed
        # by matching a finding's single `test_id` against plan.yaml test
        # ids by STRING PREFIX, which silently dropped a sibling sub-test's
        # metrics whenever the two sub-test ids do not share a prefix with
        # each other (T6.1d_dom / T6.1d_int is exactly this shape -- neither
        # is a prefix of the other).
        #
        # This oracle is deliberately NOT built from the engine's own
        # plan_test_amount_metrics()/test_id matching -- reusing that code
        # would let the very same bug re-pass this test. Instead it is a
        # hand-authored table of which of each finding's `metrics_cited`
        # names are genuine dollar-amount-at-risk figures, read directly off
        # skills/tne_exco/findings.yaml's prose (every {metric} the
        # observation text quotes as an amount) rather than off any Python
        # mapping. Independently, a metric name ending in "_max" is a
        # worst-single-instance ceiling (T6.1d's daily_over_max_dom/_int),
        # never an amount to sum -- confirmed by reading each metric's own
        # AUD-unit value in the payload, not by importing plan.yaml's `kind`.
        expected_amount_metrics_by_finding_rule_id: dict[str, set[str]] = {
            "T3_1a": {"preapproval_unlinked_amount"},
            "T3_1b": set(),
            "T3_2a": set(),
            "T3_3a": set(),
            "T3_3b": {"ent_over_amount"},
            "T4_1": {"missing_receipt_amount"},
            "T4_2": set(),
            "T4_4": {"hv_amount"},
            "T5_1": {"split_amount"},
            "T5_2": {"duplicate_amount"},
            "T6_1a": set(),
            "T6_1c": set(),
            "T6_1d": {"daily_over_amount_dom", "daily_over_amount_int"},
        }
        monetary_findings_checked = 0
        for f in payload["findings"]:
            rule_id = f["rule_id"].rsplit(".", 1)[-1]  # "SKILL-001.T6_1d" -> "T6_1d"
            expected_names = expected_amount_metrics_by_finding_rule_id[rule_id]
            cited = f.get("metrics_cited") or {}
            for name in expected_names:
                assert name in cited, f"{f['finding_id']}: expected amount metric {name!r} not cited"
                assert not name.endswith("_max"), f"{name}: a ceiling metric was listed as additive by mistake"
            if not expected_names:
                assert f["exposure_amount"] is None, f"{f['finding_id']}: non-monetary but exposure_amount set"
                continue
            monetary_findings_checked += 1
            expected = round(
                sum(cited[n]["value"] for n in expected_names if cited[n].get("value") is not None), 2,
            )
            assert f["exposure_amount"] == expected, (
                f"{f['finding_id']} ({rule_id}): exposure_amount {f['exposure_amount']} != "
                f"independently-computed sum of {sorted(expected_names)} = {expected}"
            )
        assert monetary_findings_checked > 0, "no monetary SKILL-001 finding fired on this data -- test is not exercising anything"

        # CLAUDE.md build brief P4 perf fix: get_run_frames on a snapshot-
        # backed run reads a few small Parquet files instead of re-reading
        # every full bound source (~64s locally for this real 92,798-row
        # expense_report before this change) -- must be well under a second
        # on the local backend.
        frames_started = time.time()
        frames = service.get_run_frames(ctx, run_id)
        frames_duration_s = time.time() - frames_started
        print(f"get_run_frames (snapshot-backed, local) took {frames_duration_s:.3f}s")
        assert frames_duration_s < 1.0, frames_duration_s
        assert len(frames) == 8  # one snapshot per SKILL-001 contract source
        assert len(frames["expense_report"]) < 92798  # the tested population, not the raw source
        assert "role" in frames["expense_report"].columns

        service.sign_off(ctx, run_id, "approver")
        status, _ = _wait_for_status(ctx, run_id, {"completed", "failed"}, timeout=120)
        run = service.get_run(ctx, run_id)
        assert status == "completed", run.get("status_reason")

        filename, content = service.get_export(ctx, run_id, "xlsx")
        assert filename == "workpaper.xlsx"
        assert len(content) > 1000
    finally:
        ctx.executor.stop()
