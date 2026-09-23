"""Every id in features.FEATURES must be referenced by at least one test in
this package (in a docstring, e.g. 'ID.' or 'ID: ...', or in a test
function's name). This is the check that keeps the feature inventory and
the actual test suite from drifting apart.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.e2e.features import FEATURE_IDS

THIS_DIR = Path(__file__).resolve().parent

_ID_PATTERN = re.compile(r"\b([A-Z]{1,2}\d{1,2})\b")


def _referenced_ids() -> set[str]:
    found: set[str] = set()
    for path in THIS_DIR.glob("test_*.py"):
        if path.name == "test_feature_coverage.py":
            continue
        text = path.read_text()
        found |= set(_ID_PATTERN.findall(text))
    return found & FEATURE_IDS


def test_every_inventoried_feature_is_covered_by_a_test():
    referenced = _referenced_ids()
    missing = sorted(FEATURE_IDS - referenced)
    assert not missing, f"features.py lists ids with no covering test: {missing}"


def test_feature_ids_are_well_formed_and_unique():
    from tests.e2e.features import FEATURES

    ids = [f[0] for f in FEATURES]
    assert len(ids) == len(set(ids)), "duplicate feature id in features.py"
    for fid, route, description in FEATURES:
        assert _ID_PATTERN.fullmatch(fid), f"malformed feature id: {fid!r}"
        assert route, f"{fid} has no route"
        assert description, f"{fid} has no description"
