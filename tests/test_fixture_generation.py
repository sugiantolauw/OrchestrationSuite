from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from tests.fixtures.tne_planted.generate import generate

REPO_ROOT = Path(__file__).parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "tne_planted"
PLANTS_PATH = FIXTURE_DIR / "plants.yaml"
COMMITTED_DATA_DIR = FIXTURE_DIR / "data"

pytestmark = pytest.mark.skipif(
    not COMMITTED_DATA_DIR.is_dir(), reason="tests/fixtures/tne_planted/data/ not present"
)


def test_regeneration_is_deterministic_and_reproduces_committed_data(tmp_path):
    """N2 (CLAUDE.md build brief): generate.py must produce the SAME data on
    every run given the same plants.yaml -- `hash(source)` was salted per
    Python process (PYTHONHASHSEED), so two runs of the committed generator
    could silently disagree on which background rows landed in the fixture.
    generate() now derives its per-source seed offset from sha256(source),
    which has no such salt.

    CSV (per_diem_rates, copied verbatim) is asserted byte-for-byte. XLSX is
    asserted by parsed-DataFrame equality, not bytes: xlsxwriter embeds a
    `dcterms:created`/`dcterms:modified` timestamp in `docProps/core.xml` set
    to the moment of writing, so two writes of IDENTICAL data never produce
    identical bytes -- this is xlsx container metadata, not a data
    difference, and there is no pandas/xlsxwriter option to pin it."""
    out_dir = tmp_path / "regenerated"
    generate(PLANTS_PATH, out_dir)

    committed_files = sorted(p.name for p in COMMITTED_DATA_DIR.iterdir() if p.is_file())
    regenerated_files = sorted(p.name for p in out_dir.iterdir() if p.is_file())
    assert committed_files == regenerated_files, "generate.py's output file set changed"

    for name in committed_files:
        committed_path = COMMITTED_DATA_DIR / name
        regenerated_path = out_dir / name
        if name.endswith(".csv"):
            assert committed_path.read_bytes() == regenerated_path.read_bytes(), (
                f"{name}: CSV output is not byte-for-byte reproducible"
            )
        elif name.endswith(".xlsx"):
            committed_df = pd.read_excel(committed_path)
            regenerated_df = pd.read_excel(regenerated_path)
            pd.testing.assert_frame_equal(committed_df, regenerated_df, check_like=False)
        else:
            pytest.fail(f"{name}: unexpected file type in generate.py output")


def test_regenerating_twice_into_different_dirs_agrees_on_every_dataframe(tmp_path):
    # A second, narrower determinism check independent of the committed
    # fixture: two fresh runs of generate() must agree with EACH OTHER, which
    # would already have failed under the old hash(source) salting even if
    # the committed data happened to match one particular process's hash seed.
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    generate(PLANTS_PATH, out_a)
    generate(PLANTS_PATH, out_b)

    for path_a in sorted(out_a.iterdir()):
        path_b = out_b / path_a.name
        if path_a.suffix == ".csv":
            assert path_a.read_bytes() == path_b.read_bytes()
        elif path_a.suffix == ".xlsx":
            pd.testing.assert_frame_equal(pd.read_excel(path_a), pd.read_excel(path_b))
