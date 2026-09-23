"""Unit tests for the fieldwork pipeline nodes (CLAUDE.md build brief P3 §1-2):
discover, profile, plan, execute (+ G6 reconciliation, G7 contract
conformance), classify, find, prioritise (+ exposure de-duplication, §0.3),
act, export. Uses the "mini" test Skill fixture
(tests/fixtures/skills/mini/*.yaml, owned by the P2 primitives work) with tiny
CSV data this file writes itself into tmp_path -- never into tests/fixtures/,
which this session does not own."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pandas as pd
import pytest

from orchestrator import runs as runs_module
from orchestrator.adapters.export_storage import LocalExportStorage
from orchestrator.contract import ContractViolation, LocalFileDataSource
from orchestrator.nodes.context import NodeContext
from orchestrator.nodes.fieldwork import act, classify, discover, execute, export, find, prioritise, profile
from orchestrator.skills import load_skill
from tests.conftest import canonical_ts

MINI_SKILL_DIR = Path(__file__).parent / "fixtures" / "skills" / "mini"
AUDIT_PERIOD = ("2026-01-01", "2026-02-28")


def _fingerprint(fp_id: str, skill_content_hash: str | None = None) -> dict:
    return dict(
        fingerprint_id=fp_id,
        source_table_versions="{}",
        uploaded_file_hashes="{}",
        reference_data_hashes="{}",
        skill_content_hash=skill_content_hash,
        code_revision="rev1",
        dependency_lock_hash="dep1",
        runtime_config_hash="rc1",
        endpoint_config="{}",
        prompt_template_version="none",
        created_at=canonical_ts(0),
    )


def _write_mini_data(root: Path) -> None:
    claims = pd.DataFrame(
        [
            {"Employee ID": 1, "Transaction Date": "2026-01-05", "Amount": 100, "Vendor": "VendorA"},
            {"Employee ID": 1, "Transaction Date": "2026-01-06", "Amount": 600, "Vendor": "VendorA"},
            {"Employee ID": 2, "Transaction Date": "2026-01-10", "Amount": 700, "Vendor": "VendorB"},
            {"Employee ID": 3, "Transaction Date": "2026-01-15", "Amount": 50, "Vendor": "VendorC"},
            {"Employee ID": 4, "Transaction Date": "2026-02-01", "Amount": 900, "Vendor": "VendorD"},
        ]
    )
    register = pd.DataFrame(
        [
            {"Employee ID": 1, "Transaction Date": "2026-01-05", "Vendor": "VendorA"},
            {"Employee ID": 2, "Transaction Date": "2026-01-10", "Vendor": "VendorB"},
            {"Employee ID": 4, "Transaction Date": "2026-02-01", "Vendor": "VendorD"},
        ]
    )
    claims.to_csv(root / "claims.csv", index=False)
    register.to_csv(root / "register.csv", index=False)


@dataclasses.dataclass
class Harness:
    ctx: NodeContext
    state: object
    persistence: object


def _make_harness(local_persistence, tmp_path, *, run_owner="alice", engagement_id="ENG-DEFAULT", corrupt=None):
    persistence = local_persistence
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_mini_data(data_dir)
    if corrupt:
        corrupt(data_dir)

    skill = load_skill(MINI_SKILL_DIR)
    skill.validate()
    data_source = LocalFileDataSource(root_dir=data_dir, sources=skill.contract["sources"])
    source_versions = {name: data_source.resolve_version(name) for name in skill.contract["sources"]}

    fp = _fingerprint(f"FP-{tmp_path.name}", skill.content_hash)
    now = canonical_ts(1)
    state = runs_module.create_run(
        persistence, run_kind="fieldwork", engagement_id=engagement_id, skill_id=skill.skill_id,
        skill_version=skill.version, mode="playbook", audit_period=AUDIT_PERIOD, objective="t",
        run_owner=run_owner, options={"auto_confirm_plan": True}, fingerprint=fp, now=now,
    )
    data_assets = [
        {"source": name, "table_fqn": name, "version": version}
        for name, version in source_versions.items()
    ]
    state = persistence.save_state(dataclasses.replace(state, data_assets=data_assets))

    export_dir = tmp_path / "exports"
    ctx = NodeContext(
        settings=type("S", (), {"catalog": None, "schema": None})(),
        persistence=persistence,
        data_source=data_source,
        skill=skill,
        clock=lambda: canonical_ts(2),
        export_storage=LocalExportStorage(root_dir=export_dir),
    )
    return Harness(ctx=ctx, state=state, persistence=persistence)


def _run_through_prioritise(h: Harness):
    state = discover(h.ctx, h.state)
    state = profile(h.ctx, state)
    state = execute(h.ctx, state)
    state = classify(h.ctx, state)
    state = find(h.ctx, state)
    state = prioritise(h.ctx, state)
    return state


# ── discover ──────────────────────────────────────────────────────────────────


def test_discover_confirms_bindings(local_persistence, tmp_path):
    h = _make_harness(local_persistence, tmp_path)
    state = discover(h.ctx, h.state)
    assert any(e["node"] == "discover" for e in state.events)


def test_discover_fails_on_missing_binding(local_persistence, tmp_path):
    h = _make_harness(local_persistence, tmp_path)
    stripped = dataclasses.replace(h.state, data_assets=[h.state.data_assets[0]])
    with pytest.raises(ContractViolation):
        discover(h.ctx, stripped)


def test_discover_fails_when_source_changed_since_pinning(local_persistence, tmp_path):
    h = _make_harness(local_persistence, tmp_path)
    tampered = [dict(b) for b in h.state.data_assets]
    for b in tampered:
        if b["source"] == "claims":
            b["version"] = "not-the-real-hash"
    state = dataclasses.replace(h.state, data_assets=tampered)
    with pytest.raises(ContractViolation) as exc:
        discover(h.ctx, state)
    assert "claims" in str(exc.value)


# ── profile ───────────────────────────────────────────────────────────────────


def test_profile_reports_row_and_null_counts(local_persistence, tmp_path):
    h = _make_harness(local_persistence, tmp_path)
    state = discover(h.ctx, h.state)
    state = profile(h.ctx, state)
    assert state.profile_result["claims"]["row_count"] == 5
    assert state.profile_result["claims"]["null_counts"]["Amount"] == 0
    assert state.profile_result["register"]["row_count"] == 3


# ── plan ──────────────────────────────────────────────────────────────────────


def test_plan_mirrors_skill_plan_tests(local_persistence, tmp_path):
    from orchestrator.nodes.fieldwork import plan as plan_node

    h = _make_harness(local_persistence, tmp_path)
    state = plan_node(h.ctx, h.state)
    test_ids = {t["test_id"] for t in state.plan["tests"]}
    assert test_ids == {"T1", "T2", "T3"}
    t3 = next(t for t in state.plan["tests"] if t["test_id"] == "T3")
    assert "not_testable" in t3


def test_plan_rejects_explorer_mode(local_persistence, tmp_path):
    from orchestrator.nodes.fieldwork import plan as plan_node

    h = _make_harness(local_persistence, tmp_path)
    explorer_state = dataclasses.replace(h.state, mode="explorer")
    with pytest.raises(ValueError):
        plan_node(h.ctx, explorer_state)


# ── execute + G6 reconciliation ─────────────────────────────────────────────


def test_execute_persists_metrics_and_flagged_rows(local_persistence, tmp_path):
    h = _make_harness(local_persistence, tmp_path)
    state = discover(h.ctx, h.state)
    state = execute(h.ctx, state)

    metrics = h.persistence.get_run_metrics(state.run_id)
    assert metrics["hv_count"]["value"] == 3
    assert metrics["hv_amount"]["value"] == 2200.0
    # mode: semi (mini/plan.yaml's T2) flags LEFT rows that DO have a right
    # match (a SQL semi-join), not unmatched ones -- claim1/claim3/claim5
    # each have a same-key register row; claim2/claim4 do not.
    assert metrics["missing_count"]["value"] == 3

    flagged = h.persistence.list_flagged_rows(state.run_id)
    flags = {r["flag"] for r in flagged}
    assert flags == {"RF_HV", "RF_MISSING"}
    assert sum(1 for r in flagged if r["flag"] == "RF_HV") == 3
    assert sum(1 for r in flagged if r["flag"] == "RF_MISSING") == 3

    # The mini Skill fixture (owned by the P2 primitives work) declares no
    # raw_<source> population, so the engine side of the G6 comparison is
    # unavailable here and variance is correctly None rather than falsely 0
    # (CLAUDE.md NN14 -- never claim a reconciliation that was not actually
    # computed). independent_rows (from DataSourceAdapter.row_count(), never
    # the engine's own frame) IS always available and correct.
    assert state.reconciliation["claims"]["independent_rows"] == 5
    assert state.reconciliation["claims"]["engine_rows"] is None
    assert state.reconciliation["claims"]["variance"] is None
    assert state.reconciliation["register"]["independent_rows"] == 3


def test_execute_never_touches_llm_state_fields(local_persistence, tmp_path):
    h = _make_harness(local_persistence, tmp_path)
    state = discover(h.ctx, h.state)
    state = execute(h.ctx, state)
    assert state.finding_narratives == {}
    assert state.exec_summary is None


# The mini Skill fixture declares no raw_<source> population (see the
# reconciliation assertion above), so a genuine G6 pass/fail comparison needs
# a Skill that does -- skills/tne_exco declares raw_<source> for every
# contract source. test_p3_tne_gates.py exercises G6 (pass and injected-
# failure) and G7 against that real Skill, using the small planted-exception
# fixture data (tests/fixtures/tne_planted/data/) rather than synthetic_data/
# so it runs in milliseconds, not minutes.


# ── G7 contract conformance ──────────────────────────────────────────────────


def test_g7_contract_violation_fails_the_run(local_persistence, tmp_path):
    def drop_amount_column(data_dir: Path) -> None:
        df = pd.read_csv(data_dir / "claims.csv")
        df = df.drop(columns=["Amount"])
        df.to_csv(data_dir / "claims.csv", index=False)

    h = _make_harness(local_persistence, tmp_path, corrupt=drop_amount_column)
    state = discover(h.ctx, h.state)
    with pytest.raises(ContractViolation) as exc:
        execute(h.ctx, state)
    assert "Amount" in str(exc.value)


# ── classify / find ───────────────────────────────────────────────────────────


def test_classify_summarises_exceptions(local_persistence, tmp_path):
    h = _make_harness(local_persistence, tmp_path)
    state = discover(h.ctx, h.state)
    state = execute(h.ctx, state)
    state = classify(h.ctx, state)
    by_id = {e["test_id"]: e for e in state.exceptions}
    assert by_id["T1"]["status"] == "exception"
    assert by_id["T1"]["exception_units"] == 3
    assert by_id["T3"]["status"] == "not_testable"


def test_find_builds_findings_from_persisted_metrics(local_persistence, tmp_path):
    h = _make_harness(local_persistence, tmp_path)
    state = discover(h.ctx, h.state)
    state = execute(h.ctx, state)
    state = classify(h.ctx, state)
    state = find(h.ctx, state)

    persisted = h.persistence.list_findings(state.run_id)
    by_test = {f["test_id"]: f for f in persisted}
    assert by_test["T1"]["severity"] == "High"
    assert by_test["T2"]["severity"] == "Low"
    assert {f["finding_id"] for f in state.findings} == {f["finding_id"] for f in persisted}

    issues = h.persistence.list_findings(state.run_id)  # sanity: still readable
    assert len(issues) == 2


def test_find_is_idempotent_on_reexecution(local_persistence, tmp_path):
    h = _make_harness(local_persistence, tmp_path)
    state = discover(h.ctx, h.state)
    state = execute(h.ctx, state)
    state = find(h.ctx, state)
    first = h.persistence.list_findings(state.run_id)

    state2 = find(h.ctx, state)
    second = h.persistence.list_findings(state2.run_id)
    assert [f["finding_id"] for f in first] == [f["finding_id"] for f in second]
    assert len(second) == 2  # never duplicated


# ── prioritise: exposure de-duplication (CLAUDE.md §0.3) ────────────────────


def test_prioritise_dedupes_exposure_across_findings(local_persistence, tmp_path):
    h = _make_harness(local_persistence, tmp_path)
    state = _run_through_prioritise(h)

    by_test = {f["test_id"]: f for f in state.findings}
    # RF_HV = claim2($600)+claim3($700)+claim5($900); RF_MISSING (mode: semi
    # flags rows that DO match the register) = claim1($100)+claim3($700)+
    # claim5($900). claim3 and claim5 are cited by BOTH findings -- the
    # naive sum-of-per-finding-totals would double count them.
    assert by_test["T1"]["exposure_amount"] == 2200.0  # claim2+claim3+claim5
    assert by_test["T2"]["exposure_amount"] == 1700.0  # claim1+claim3+claim5

    headline = h.persistence.get_run_metrics(state.run_id)["run_exposure_headline"]
    naive_sum = by_test["T1"]["exposure_amount"] + by_test["T2"]["exposure_amount"]
    assert naive_sum == 3900.0
    assert headline["value"] == 2300.0  # union {claim1,claim2,claim3,claim5}, not the naive sum
    assert headline["value"] < naive_sum


def test_prioritise_orders_by_severity_then_exposure(local_persistence, tmp_path):
    h = _make_harness(local_persistence, tmp_path)
    state = _run_through_prioritise(h)
    severities = [f["severity"] for f in state.findings]
    assert severities == sorted(severities, key=lambda s: {"High": 0, "Medium": 1, "Low": 2}[s])


# ── act ───────────────────────────────────────────────────────────────────────


def test_act_drafts_one_action_per_finding(local_persistence, tmp_path):
    h = _make_harness(local_persistence, tmp_path)
    state = _run_through_prioritise(h)
    state = act(h.ctx, state)

    actions = h.persistence.list_management_actions(filters={"run_id": state.run_id})
    assert len(actions) == 2
    assert all(a["status"] == "draft" for a in actions)
    assert {a["finding_id"] for a in actions} == {f["finding_id"] for f in state.findings}


# ── export ────────────────────────────────────────────────────────────────────


def test_export_writes_xlsx_and_records_it(local_persistence, tmp_path):
    h = _make_harness(local_persistence, tmp_path)
    state = _run_through_prioritise(h)
    state = act(h.ctx, state)
    state = export(h.ctx, state)

    assert "xlsx" in state.exports
    path = Path(state.exports["xlsx"]["path"])
    assert path.is_file()
    assert path.stat().st_size > 0

    recorded = h.persistence.list_exports(state.run_id)
    assert len(recorded) == 1
    assert recorded[0]["sha256"] == state.exports["xlsx"]["sha256"]

    import hashlib

    assert hashlib.sha256(path.read_bytes()).hexdigest() == recorded[0]["sha256"]
