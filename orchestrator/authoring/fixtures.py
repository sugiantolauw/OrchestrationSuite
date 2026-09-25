"""docs/specs/P7_mapping_authoring_design.md §2.3 "Planted-fixture generator
(CLAUDE.md §9: never its own oracle)". Generates a Skill's fixture data
DIRECTLY from a hand-authored `plants.yaml` sidecar -- the sidecar IS the
oracle (declared exceptions/negatives), never inferred from anything this
module does. A contract column with no declared value in a non-nullable
position is a generator error, never a guessed value (NN14 applies to
fixtures too, §2.3)."""

from __future__ import annotations

import hashlib
import random
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yaml


class FixtureGenerationError(Exception):
    pass


def _seed_for(seed: int, *parts: str) -> int:
    """A seed derived from sha256(seed, *parts) -- never Python's own
    `hash()`, which changes per process under PYTHONHASHSEED (the G9
    lesson, CLAUDE.md §5; `tests/test_fixture_generation.py`'s namesake
    SKILL-001 generator applies the same rule)."""
    digest = hashlib.sha256(f"{seed}:{':'.join(parts)}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def _vary_value(rng: random.Random, spec: dict):
    if "choice" in spec:
        return rng.choice(spec["choice"])
    if "int_range" in spec:
        lo, hi = spec["int_range"]
        return rng.randint(lo, hi)
    if "decimal_range" in spec:
        lo, hi = spec["decimal_range"]
        return round(rng.uniform(lo, hi), 2)
    if "date_range" in spec:
        lo, hi = (date.fromisoformat(v) for v in spec["date_range"])
        span = max((hi - lo).days, 0)
        return (lo + timedelta(days=rng.randint(0, span))).isoformat()
    raise FixtureGenerationError(f"plants.yaml vary spec has no recognised key: {spec!r}")


def _rows_for_background(seed: int, source: str, cfg: dict, natural_id_column: str) -> list[dict]:
    rng = random.Random(_seed_for(seed, source, "background"))
    rows: list[dict] = []
    template = cfg.get("template") or {}
    vary = cfg.get("vary") or {}
    for i in range(cfg["count"]):
        row = dict(template)
        for col, spec in vary.items():
            row[col] = _vary_value(rng, spec)
        row[natural_id_column] = f"BG-{source}-{i + 1}"
        rows.append(row)
    return rows


def _rows_for_tests(plants: dict, natural_id_column: str) -> dict[str, list[dict]]:
    by_source: dict[str, list[dict]] = {}
    for test_id, block in (plants.get("tests") or {}).items():
        if test_id.startswith("_"):
            continue
        source = block["source"]
        if block.get("scoring_unit") == "row":
            for entry in block.get("rows", []):
                row = dict(entry["fields"])
                row[natural_id_column] = entry["natural_id"]
                by_source.setdefault(source, []).append(row)
        else:
            for group in block.get("groups", []):
                for member in group.get("members", []):
                    row = dict(member["fields"])
                    row[natural_id_column] = member["natural_id"]
                    by_source.setdefault(source, []).append(row)
    return by_source


def _check_non_nullable_declared(source: str, cfg: dict, rows: list[dict], natural_id_column: str) -> None:
    for col, spec in cfg.get("columns", {}).items():
        if spec.get("nullable", True):
            continue
        for row in rows:
            if row.get(col) is None:
                raise FixtureGenerationError(
                    f"source {source!r}: non-nullable contract column {col!r} has no declared "
                    f"value for generated row (natural_id={row.get(natural_id_column)!r}) -- "
                    f"never filled with a guessed value (NN14)"
                )


def generate_fixtures(skill_dir: str | Path, plants_path: str | Path, out_dir: str | Path) -> dict[str, list[dict]]:
    """Writes every contract source's generated rows to `out_dir` in that
    source's own declared `format`/`file`/`sheet`, and returns
    {source: [row dict, ...]} -- exactly what was written, for a caller
    (the CLI, the notebook, tests) to report row counts without re-reading
    the files. Every non-nullable contract column must have a declared
    value on every generated row for that source (background template/vary,
    or a test's own `fields`) -- undeclared is a FixtureGenerationError,
    never a silent default."""
    skill_dir = Path(skill_dir)
    plants = yaml.safe_load(Path(plants_path).read_text())
    seed = plants["seed"]
    natural_id_column = plants["natural_id_column"]
    contract_sources = yaml.safe_load((skill_dir / "contract.yaml").read_text())["sources"]

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows_by_source: dict[str, list[dict]] = {name: [] for name in contract_sources}
    for source, cfg in (plants.get("background") or {}).items():
        rows_by_source.setdefault(source, []).extend(_rows_for_background(seed, source, cfg, natural_id_column))
    for source, rows in _rows_for_tests(plants, natural_id_column).items():
        rows_by_source.setdefault(source, []).extend(rows)

    written: dict[str, list[dict]] = {}
    for source, cfg in contract_sources.items():
        rows = rows_by_source.get(source, [])
        _check_non_nullable_declared(source, cfg, rows, natural_id_column)
        df = pd.DataFrame(rows)
        path = out_dir / cfg["file"]
        fmt = cfg.get("format", "csv")
        if fmt == "csv":
            df.to_csv(path, index=False)
        elif fmt == "xlsx":
            sheet = cfg.get("sheet") or "Sheet1"
            df.to_excel(path, sheet_name=str(sheet), index=False)
        else:
            raise FixtureGenerationError(f"source {source!r}: unsupported format {fmt!r} for fixture generation")
        written[source] = rows

    return written
