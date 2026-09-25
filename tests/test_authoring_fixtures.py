"""docs/specs/P7_mapping_authoring_design.md §2.4: deterministic across two
subprocesses; undeclared non-nullable column -> error; output passes
validate_contract; a background row that trips a test is scored as a
false positive; the generic scorer's arithmetic cross-checked against
`orchestrator.eval.surface2` on the real SKILL-001 committed fixture."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from orchestrator.authoring.fixtures import FixtureGenerationError, generate_fixtures
from orchestrator.authoring.score import score_fixtures
from orchestrator.contract import LocalFileDataSource, validate_contract

MINI = Path(__file__).parent / "fixtures" / "skills" / "mini"
MINI_PLANTS = MINI / "plants.yaml"
REPO_ROOT = Path(__file__).parent.parent


def _copy_mini(tmp_path: Path) -> Path:
    dest = tmp_path / "mini"
    shutil.copytree(MINI, dest)
    return dest


def test_generation_is_deterministic_across_two_subprocesses(tmp_path: Path):
    out1, out2 = tmp_path / "run1", tmp_path / "run2"
    for out in (out1, out2):
        result = subprocess.run(
            [sys.executable, "-m", "orchestrator.authoring", "generate-fixtures",
             str(MINI), "--plants", str(MINI_PLANTS), "--out", str(out)],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr

    for name in ("claims.csv", "register.csv"):
        assert (out1 / name).read_bytes() == (out2 / name).read_bytes(), name


def test_undeclared_non_nullable_column_is_a_generator_error(tmp_path: Path):
    dest = _copy_mini(tmp_path)
    plants = yaml.safe_load(MINI_PLANTS.read_text())
    del plants["tests"]["T1"]["rows"][0]["fields"]["Vendor"]  # Vendor is non-nullable on claims
    plants_path = dest / "plants.yaml"
    plants_path.write_text(yaml.safe_dump(plants))

    with pytest.raises(FixtureGenerationError, match="Vendor"):
        generate_fixtures(dest, plants_path, tmp_path / "data")


def test_generated_output_passes_validate_contract(tmp_path: Path):
    written = generate_fixtures(MINI, MINI_PLANTS, tmp_path / "data")
    assert set(written) == {"claims", "register"}

    contract_sources = yaml.safe_load((MINI / "contract.yaml").read_text())["sources"]
    data_source = LocalFileDataSource(root_dir=tmp_path / "data", sources=contract_sources)
    for source, cfg in contract_sources.items():
        version = data_source.resolve_version(source)
        df = data_source.read_population(source, version=version)
        validate_contract(df, cfg)  # raises ContractViolation on failure


def test_a_background_row_that_trips_a_test_is_scored_as_a_false_positive(tmp_path: Path):
    dest = _copy_mini(tmp_path)
    plants = yaml.safe_load(MINI_PLANTS.read_text())
    # Widen T1's claims background so some background rows exceed the
    # hv_limit (500) it is supposed to stay clean of.
    plants["background"]["claims"]["vary"]["Amount"]["decimal_range"] = [10, 900]
    plants_path = dest / "plants.yaml"
    plants_path.write_text(yaml.safe_dump(plants))

    data_dir = tmp_path / "data"
    generate_fixtures(dest, plants_path, data_dir)
    scores = score_fixtures(dest, plants_path, data_dir)

    t1 = scores["T1"]
    assert t1.false_positives > 0
    assert all(fp_id.startswith("BG-claims-") for fp_id in t1.false_positive_ids)
    # The 3 declared plants must still be found -- widening background noise
    # never suppresses a real exception.
    assert t1.true_positives == 3
    assert t1.false_negatives == 0


# ── cross-check against orchestrator.eval.surface2 (§2.3) ──────────────────

SKILL001_DIR = REPO_ROOT / "skills" / "tne_exco"
TNE_PLANTS = REPO_ROOT / "tests" / "fixtures" / "tne_planted" / "plants.yaml"
TNE_DATA = REPO_ROOT / "tests" / "fixtures" / "tne_planted" / "data"
TNE_AUDIT_PERIOD = ("2025-01-01", "2026-04-30")

pytestmark_skill001 = pytest.mark.skipif(
    not TNE_DATA.is_dir(), reason="tests/fixtures/tne_planted/data/ not present -- run generate.py first"
)


@pytestmark_skill001
def test_generic_scorer_arithmetic_matches_surface2_on_skill001_committed_fixture():
    """Both scorers' precision/recall/TP/FP/FN arithmetic, cross-checked
    test-by-test against real SKILL-001 execution: `orchestrator.eval.
    surface2` resolves each test's own expected_true/flagged sets (row
    natural ids via its per-source functions, or group ids via its per-test
    gid functions), and this feeds the SAME resolved sets into
    `orchestrator.authoring.score._score` -- the generic scorer's shared
    comparison function -- asserting field-for-field equality against
    `run_surface2`'s own recorded TestScore.

    This is narrower than an end-to-end run of `score_fixtures` against
    SKILL-001 (impossible: SKILL-001's real data has no `natural_id_column`
    -- §2.3's generic sidecar shape is for small, self-contained fixtures,
    not a 150K-row shared population), but it is the part that can
    genuinely diverge between the two modules: the counting rule itself.
    Every scorable test's flagged set is a SUPERSET of its own declared
    plants (0 FN throughout, per tests/test_surface2.py) -- surface2's
    SCOPING (restricting to each test's own declared plants/negatives,
    CLAUDE.md P2/P3 gate review item B1) is a no-op for most tests, and the
    generic scorer's stricter, unscoped rule (§2.3: "anything flagged that
    no plant declares counts as a false positive") gives IDENTICAL results
    for those. A few tests (e.g. T4.4) legitimately flag rows planted for a
    DIFFERENT test that also satisfy T4.4's own rule (surface2.py's own
    docstring example: a >$5,000 line planted for T5.1's max_line exclusion
    is also a genuine T4.4 high-value exception) -- there the two scorers'
    counting rules genuinely diverge, by design, not by bug: these are
    detected (every extra flagged unit lies outside this test's own
    plants.yaml scope) and excluded from the strict equality assertion,
    named in `excluded`, rather than silently skipped or asserted equal.
    No test's scoring unit is unclassifiable: every SKILL-001 source
    resolves through surface2's own `_NATURAL_ID_FN` (row-grain) or
    `_GROUP_GID_FN` (group-grain) dicts, asserted via `unmatched`."""
    from orchestrator.authoring.score import _score
    from orchestrator.eval import surface2

    skill = surface2.load_skill(SKILL001_DIR)
    skill.validate()
    plants_doc = yaml.safe_load(TNE_PLANTS.read_text())
    data_source = surface2.LocalFileDataSource(root_dir=TNE_DATA, sources=skill.contract["sources"])
    result = surface2.execute_skill(
        skill, data_source=data_source, audit_period=TNE_AUDIT_PERIOD, run_context={"run_id": "authoring-cross-check"}
    )
    official = surface2.run_surface2(
        skill_dir=SKILL001_DIR, plants_path=TNE_PLANTS, data_dir=TNE_DATA,
        audit_period=TNE_AUDIT_PERIOD, skill=skill,
    )

    test_blocks = {tid: b for tid, b in plants_doc["tests"].items() if not tid.startswith("_")}
    row_sources = {b["source"] for b in test_blocks.values() if b["source"] in surface2._NATURAL_ID_FN}
    row_maps = {
        s: surface2._row_key_natural_id_map(skill, data_source, s, result.source_versions[s]) for s in row_sources
    }
    reverse_row_maps = {s: {nid: rk for rk, nid in m.items()} for s, m in row_maps.items()}

    unmatched: list[str] = []
    excluded: list[tuple[str, str]] = []
    compared = 0
    for test_id, block in test_blocks.items():
        if test_id in surface2._GROUP_GID_FN:
            gid_fn = surface2._GROUP_GID_FN[test_id]
            if test_id in surface2._GROUP_GID_NEEDS_ROW_KEY_LOOKUP:
                row_key_of = reverse_row_maps[block["source"]].__getitem__
                expected_true = {gid_fn(p, row_key_of=row_key_of) for p in block["plants"]}
                expected_false = {gid_fn(n, row_key_of=row_key_of) for n in block["negatives"]}
            else:
                expected_true = {gid_fn(p) for p in block["plants"]}
                expected_false = {gid_fn(n) for n in block["negatives"]}
            flagged = set(result.scored_units.get(test_id, []))
            mine = _score(test_id, "group", expected_true, flagged, n_negatives=len(block["negatives"]))
        elif block["source"] in surface2._NATURAL_ID_FN:
            row_map = row_maps[block["source"]]
            expected_true = {p["natural_id"] for p in block["plants"]}
            expected_false = {n["natural_id"] for n in block["negatives"]}
            flagged_row_keys = set(result.scored_units.get(test_id, []))
            flagged = {row_map[rk] for rk in flagged_row_keys if rk in row_map}
            mine = _score(test_id, "row", expected_true, flagged, n_negatives=len(block["negatives"]))
        else:
            unmatched.append(test_id)
            continue

        out_of_scope = flagged - (expected_true | expected_false)
        if out_of_scope:
            excluded.append((
                test_id,
                f"{len(out_of_scope)} unit(s) flagged that this test's own plants.yaml block does not "
                f"declare -- legitimately caught by another test's plant (surface2.py's own scoping, "
                f"CLAUDE.md P2/P3 gate review item B1); the generic scorer's unscoped rule diverges here "
                f"by design",
            ))
            continue

        compared += 1
        want = official[test_id].to_dict()
        got = mine.to_dict()
        for field_name in ("true_positives", "false_positives", "false_negatives", "precision", "recall"):
            assert got[field_name] == want[field_name], (
                f"{test_id}.{field_name}: generic scorer={got[field_name]!r}, surface2={want[field_name]!r}"
            )

    assert not unmatched, f"tests this cross-check's adapter could not classify as row- or group-grain: {unmatched}"
    assert compared > 0, "every scorable test was excluded -- the cross-check compared nothing"
    print(f"cross-check: {compared} test(s) compared exactly, {len(excluded)} excluded: {excluded}")
