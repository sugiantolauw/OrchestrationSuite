from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from orchestrator.skills import Skill


@dataclass
class NodeContext:
    """Everything a fieldwork node needs, threaded through `run_phase`'s `skill`
    argument (CLAUDE.md §2.2 -- the pipeline loop is generic; a node's real
    dependencies are whatever this context carries, never module globals and
    never the executor or Dash, CLAUDE.md §2.5). A node's signature is
    `fn(ctx: NodeContext, state: RunState) -> RunState`.

    `data_source` is this RUN's bound DataSourceAdapter (CLAUDE.md §3.16 --
    UCTableDataSource or LocalFileDataSource, resolved once at run creation from
    the run's bindings, never re-selected per node). `export_storage` is only
    used by the `export` node."""

    settings: Any
    persistence: Any
    data_source: Any
    skill: Skill
    clock: Callable[[], str]
    export_storage: Any = None
    tracer: Any = None
