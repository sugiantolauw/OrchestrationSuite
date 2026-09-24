"""Lazy node registry (LIFECYCLE_design.md §2.4; CLAUDE.md §3 non-negotiable
1's NN1 exception confinement). Maps `run_kind -> phase -> [(node_name,
"module:function")]` and resolves each dotted path -- importing the module
and looking up the function -- only the first time that `run_kind` is
actually looked up, never at import time of this module or of
`orchestrator.nodes`.

This is what LI-5 (LIFECYCLE_design.md §1) requires at runtime: a process
that only ever runs `fieldwork` never imports `orchestrator.lifecycle`,
`orchestrator.research`, `orchestrator.knowledge` or any other run_kind's
node module, even once those packages exist -- because nothing here imports
`orchestrator.nodes.fieldwork` (or a future `orchestrator.nodes.sensing`,
etc.) until a run of that specific kind is created and its nodes are
resolved. It also means a lifecycle module that fails to import breaks only
runs of its own run_kind, never fieldwork.

`executor.py` and `service.py` import `NODES_FOR` from here instead of from
`orchestrator.nodes.fieldwork` directly (`orchestrator/nodes/__init__.py`
does too, since a package's `__init__.py` runs on any submodule import and
must not itself defeat the laziness this module exists for).

`NODES_FOR` is drop-in compatible with the plain dict it replaces: it
implements `Mapping`, so `NODES_FOR[run_kind]`, `NODES_FOR.get(run_kind,
default)` and `NODES_FOR[run_kind][phase]` all behave exactly as they did
against the literal `{"fieldwork": {...}}` dict fieldwork.py used to export,
except that `NODES_FOR[run_kind]` triggers (and then caches) the import of
that run_kind's node module the first time it is looked up.

Only `fieldwork` has a real node module today. The other run_kinds
(`sensing`, `assessment`, `planning`, `design_assessment`, `reporting`,
`evidence`) arrive with their own modules in later lifecycle work packages
(L2-L8) -- looking one of them up before then raises KeyError, same as it
would against a dict that never had that key.
"""

from __future__ import annotations

import importlib
import threading
from collections.abc import Mapping
from typing import Callable

NodeFn = Callable[..., object]

# run_kind -> phase -> [(node_name, "module.path:function_name")]. The dotted
# path is a plain string, never an `import` statement, so listing a future
# run_kind's node module here (once it exists) still does not import it --
# only NodesForRegistry.__getitem__ does that, and only for the run_kind
# actually being looked up.
_NODE_SPECS: dict[str, dict[str, list[tuple[str, str]]]] = {
    "fieldwork": {
        "plan": [
            ("discover", "orchestrator.nodes.fieldwork:discover"),
            ("profile", "orchestrator.nodes.fieldwork:profile"),
            ("plan", "orchestrator.nodes.fieldwork:plan"),
        ],
        "execute": [
            ("execute", "orchestrator.nodes.fieldwork:execute"),
            ("classify", "orchestrator.nodes.fieldwork:classify"),
            ("find", "orchestrator.nodes.fieldwork:find"),
            ("prioritise", "orchestrator.nodes.fieldwork:prioritise"),
            ("narrate", "orchestrator.nodes.narration:narrate"),
            ("act", "orchestrator.nodes.fieldwork:act"),
        ],
        "export": [
            # P6 WP N10 (docs/specs/P6_narration_design.md §2): `finalise`
            # copies accepted AI-proposed findings and recomputes the
            # headline before `export` reads them -- see nodes/fieldwork.py's
            # own NODES_FOR comment (this list must never drift from it,
            # tests/test_lifecycle_isolation.py::
            # test_registry_fieldwork_sequence_matches_fieldwork_nodes_for).
            ("finalise", "orchestrator.nodes.narration:finalise"),
            ("export", "orchestrator.nodes.fieldwork:export"),
        ],
    }
}


def _resolve(spec: str) -> NodeFn:
    module_path, _, func_name = spec.partition(":")
    module = importlib.import_module(module_path)
    return getattr(module, func_name)


class NodesForRegistry(Mapping):
    def __init__(self, specs: dict[str, dict[str, list[tuple[str, str]]]] | None = None):
        self._specs = specs if specs is not None else _NODE_SPECS
        self._resolved: dict[str, dict[str, list[tuple[str, NodeFn]]]] = {}
        self._lock = threading.Lock()

    def __getitem__(self, run_kind: str) -> dict[str, list[tuple[str, NodeFn]]]:
        cached = self._resolved.get(run_kind)
        if cached is not None:
            return cached
        if run_kind not in self._specs:
            raise KeyError(run_kind)
        with self._lock:
            cached = self._resolved.get(run_kind)
            if cached is None:
                cached = {
                    phase: [(name, _resolve(spec)) for name, spec in entries]
                    for phase, entries in self._specs[run_kind].items()
                }
                self._resolved[run_kind] = cached
        return cached

    def __iter__(self):
        return iter(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, run_kind: object) -> bool:
        return run_kind in self._specs


# The one process-wide registry every caller shares: a single instance,
# imported everywhere, so a run_kind resolved once by any caller is cached
# for every other caller in this process too.
NODES_FOR = NodesForRegistry()
