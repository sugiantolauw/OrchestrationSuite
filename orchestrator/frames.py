"""Per-run row snapshots for /workspace/tne (CLAUDE.md build brief P4 perf
fix): before this module, the workspace's first callback for a run called
orchestrator.service.get_run_frames, which re-read every FULL bound source at
its pinned version -- ~39s on the deployed App, ~64s locally, for a single
callback (CLAUDE.md §2.1: "nothing over a couple of seconds runs in a
callback"). The `execute` node (orchestrator/nodes/fieldwork.py) now calls
`build_row_snapshots` right after it has the engine's ExecutionResult and
flagged_rows in hand, and writes one small Parquet file per contract source
-- only the rows the run's populations actually tested, not the whole raw
source -- through the run's ExportStorageAdapter. get_run_frames then reads
those files back instead of re-reading source data.

Both `tested_population_names` and the `frame_tags` mechanism are Skill-data
driven (CLAUDE.md §4.4/§4.6: "generic pipeline code reading plan.yaml, never
a per-Skill method") -- nothing here names a Skill, a source or a population
literally. `tested_population_names` uses the exact same
(population/left_population/right_population) param-key convention
orchestrator.skills.validate_skill already uses to find every population a
test actually reads (skills.py:266-269) -- not a new convention. `frame_tags`
is a small, optional, declarative plan.yaml block a Skill uses to tag rows of
one of its snapshots with which of several (usually overlapping) populations
a row belongs to -- SKILL-001 uses it once, for T&E's P_EXP
prepared/approved/both role (spec §0), which restores the prototype's
"colour by role" spend-by-employee chart (reference_app/app.py's
Source_Population column) now that a real per-row role is available:

    frame_tags:
      role:
        source: expense_report
        from:            # checked in this order -- first match wins
          both: p_exp_both_all
          prepared: p_exp_prepared_all
          approved: p_exp_approved_all
"""

from __future__ import annotations

import hashlib
import io

import pandas as pd

from orchestrator.engine import ExecutionResult
from orchestrator.skills import Skill


def not_testable_flags(skill: Skill) -> set[str]:
    """Every RF_* flag a not_testable test declares (plan.yaml), regardless of
    which contract source it would have applied to -- get_run_frames has
    always broadcast these NA columns to every source's frame rather than
    scoping them (service.py's own prior _not_testable_flags did the same),
    and this keeps that behaviour unchanged (CLAUDE.md NN15)."""
    flags: set[str] = set()
    for t in skill.plan.get("tests", []):
        if "not_testable" in t:
            flags.update(t["not_testable"].get("flags", []))
    return flags


def tested_population_names(skill: Skill) -> set[str]:
    """Every population name a real (non-not_testable) test actually reads --
    the same population/left_population/right_population param-key scan
    orchestrator.skills.validate_skill uses to check those names resolve. A
    population.yaml entry never read by any primitive (e.g. the `raw_*`
    reconciliation-only populations, minus the one -- T4.2's
    `right_population: raw_attendee_validity` -- that IS read despite its
    name) contributes no rows to a snapshot; this is why row-key membership
    is checked by ACTUAL usage, never by a `raw_` prefix convention."""
    names: set[str] = set()
    for t in skill.plan.get("tests", []):
        if "not_testable" in t:
            continue
        params = t.get("params", {}) or {}
        for key in ("population", "left_population", "right_population"):
            name = params.get(key)
            if name:
                names.add(name)
    return names


def build_row_snapshots(
    skill: Skill, result: ExecutionResult, flagged_rows: list[dict]
) -> dict[str, pd.DataFrame]:
    """One DataFrame per contract source: that source's own rows (raw column
    order, contract-typed, __source/__row_key intact) restricted to the union
    of __row_key values across every population a test actually read from
    that source, plus one 0/1 (Int64, <NA> for not_testable) column per RF_*
    flag, plus any skill.plan.yaml `frame_tags` columns declared for that
    source. Row order is the source file's own order (boolean masking
    preserves it) -- deterministic and stable across processes (G9)."""
    tested = tested_population_names(skill)
    frame_tags = skill.plan.get("frame_tags", {}) or {}
    nt_flags = not_testable_flags(skill)

    rows_by_source: dict[str, list[dict]] = {}
    for r in flagged_rows:
        rows_by_source.setdefault(r["source"], []).append(r)

    snapshots: dict[str, pd.DataFrame] = {}
    for source, raw_df in result.raw_frames.items():
        row_keys: set[str] = set()
        for name in tested:
            pop = result.population_objects.get(name)
            if pop is not None and pop.source == source:
                row_keys.update(pop.df["__row_key"].tolist())

        snap = raw_df[raw_df["__row_key"].isin(row_keys)].copy().reset_index(drop=True)

        rows = rows_by_source.get(source, [])
        flags_present = sorted({r["flag"] for r in rows})
        for flag in flags_present:
            keys = {r["row_key"] for r in rows if r["flag"] == flag}
            snap[flag] = snap["__row_key"].isin(keys).astype("Int64")
        for flag in sorted(nt_flags):
            if flag not in snap.columns:
                snap[flag] = pd.array([pd.NA] * len(snap), dtype="Int64")

        for tag_name, cfg in sorted(frame_tags.items()):
            if cfg.get("source") != source:
                continue
            tag_values = pd.Series([pd.NA] * len(snap), index=snap.index, dtype="object")
            for label, pop_name in cfg.get("from", {}).items():
                pop = result.population_objects.get(pop_name)
                if pop is None:
                    continue
                keys = set(pop.df["__row_key"].tolist())
                still_untagged = tag_values.isna()
                tag_values[snap["__row_key"].isin(keys) & still_untagged] = label
            snap[tag_name] = tag_values

        snapshots[source] = snap

    return snapshots


def frame_parquet_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return buf.getvalue()


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class FrameSnapshotIntegrityError(Exception):
    """A recorded frame snapshot's sha256 does not match the bytes read back
    from export storage -- CLAUDE.md NN14: never silently re-read the source
    instead, the run's recorded evidence no longer matches what is stored."""

    def __init__(self, source: str, path: str, expected: str, actual: str):
        self.source = source
        self.path = path
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"frame snapshot for {source!r} at {path!r} failed integrity check: "
            f"recorded sha256={expected!r}, bytes read back hash to {actual!r}"
        )


def read_frame_parquet(export_storage, *, source: str, path: str, expected_sha256: str) -> pd.DataFrame:
    content = export_storage.read(path)
    actual = sha256_bytes(content)
    if actual != expected_sha256:
        raise FrameSnapshotIntegrityError(source, path, expected_sha256, actual)
    return pd.read_parquet(io.BytesIO(content))
