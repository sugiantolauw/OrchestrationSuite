from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from orchestrator.skills import Skill


class _CachingDataSource:
    """Wraps a run's DataSourceAdapter so `read_population()` results are
    memoized per (source, version, columns, filters) for the life of this
    NodeContext -- i.e. one executor pass (CLAUDE.md build brief P3 §3):
    discover/profile/execute/prioritise each read the same bound sources at
    the same pinned versions within a single pass, and without this a 30MB
    local xlsx source was parsed 3-4 times per pass.

    `row_count()` (and every other DataSourceAdapter method) is deliberately
    left untouched -- delegated straight through to the wrapped adapter via
    __getattr__, never routed through this cache. G6 (fieldwork.execute's own
    docstring, CLAUDE.md §5) requires row_count() to be an INDEPENDENTLY
    obtained count, never one derived from the same in-memory frame the
    engine's populations were built from; short-circuiting it through this
    cache would defeat that independence and silently weaken the gate.

    Returns a copy of the cached frame on every call so a caller mutating its
    own DataFrame in place (e.g. `df[col] = ...`) can never corrupt what a
    later cache hit hands back to a different node."""

    def __init__(self, inner: Any):
        self._inner = inner
        self._cache: dict[tuple, Any] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def read_population(
        self,
        source: str,
        *,
        version,
        columns: list[str] | None = None,
        filters: dict[str, Any] | None = None,
    ):
        key = (
            source,
            version,
            tuple(sorted(columns)) if columns else None,
            json.dumps(filters, sort_keys=True, default=str) if filters else None,
        )
        if key not in self._cache:
            self._cache[key] = self._inner.read_population(
                source, version=version, columns=columns, filters=filters
            )
        return self._cache[key].copy()


@dataclass
class NodeContext:
    """Everything a fieldwork node needs, threaded through `run_phase`'s `skill`
    argument (CLAUDE.md §2.2 -- the pipeline loop is generic; a node's real
    dependencies are whatever this context carries, never module globals and
    never the executor or Dash, CLAUDE.md §2.5). A node's signature is
    `fn(ctx: NodeContext, state: RunState) -> RunState`.

    `data_source` is this RUN's bound DataSourceAdapter (CLAUDE.md §3.16 --
    UCTableDataSource or LocalFileDataSource, resolved once at run creation from
    the run's bindings, never re-selected per node), wrapped in
    `_CachingDataSource` on construction so every node sharing this one
    NodeContext (i.e. one executor pass, CLAUDE.md build brief P3 §3) sees the
    same read_population() result rather than each re-reading. `export_storage`
    is only used by the `export` node.

    `backend` is "local" or "uc" (AppContext.backend, CLAUDE.md build brief
    P3 §3's data_mode label -- "Local test data" vs "Unity Catalog"), threaded
    through so `export`'s PPTX cover slide can state which without a node
    branching on Dash/service-layer state it has no other access to. Defaults
    to "uc" so every existing direct NodeContext(...) construction (tests
    that never pass it) keeps meaning what it already implied."""

    settings: Any
    persistence: Any
    data_source: Any
    skill: Skill
    clock: Callable[[], str]
    export_storage: Any = None
    tracer: Any = None
    backend: str = "uc"
    # Independent review 2026-09-24 item 4: the ModelClient a node's LLM
    # calls use. None means "build a real DatabricksModelClient lazily"
    # (orchestrator.nodes.fieldwork._build_classify_gateway) -- a test
    # passes a FakeModelClient/RaisingModelClient here instead. Currently
    # only consulted by the T4.3 row-level classification capability, which
    # itself only runs when Settings.enable_row_level_llm is true.
    model_client: Any = None

    def __post_init__(self) -> None:
        if self.data_source is not None and not isinstance(self.data_source, _CachingDataSource):
            self.data_source = _CachingDataSource(self.data_source)
