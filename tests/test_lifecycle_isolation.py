"""L0.2 isolation tests (LIFECYCLE_design.md §1 LI-5, §2.4; CLAUDE.md §3
non-negotiable 1). NN1's stated exception -- a bounded agentic loop may be
used for the research module -- is confined to `orchestrator.research`,
`orchestrator.knowledge`, `orchestrator.lifecycle` and the lifecycle
run_kinds' own node modules (`orchestrator.nodes.sensing`, `.assessment`,
`.planning`, `.design`, `.reporting`, `.evidence`, per
LIFECYCLE_design.md §2.1's package layout). Nothing in the fieldwork
pipeline may import any of them, even transitively, and the lazy node
registry (`orchestrator/nodes/registry.py`) is what makes that true at
runtime: it resolves and imports a run_kind's node module only when a run of
that kind is actually looked up, so a process that only ever runs fieldwork
never imports another run_kind's node module at all.

Two independent checks, matching the work package's own wording ("AST plus
sys.modules"):

  - `test_fieldwork_import_graph_never_reaches_forbidden_packages` walks the
    real import graph reachable from orchestrator/nodes/fieldwork.py
    statically (via `ast`, confined to files under orchestrator/, never
    actually importing anything) and asserts it never reaches a forbidden
    module.
  - `test_registry_import_alone_is_lazy_and_resolving_fieldwork_stays_isolated`
    runs tests/lifecycle_isolation_worker.py as a real subprocess, which
    resolves the real fieldwork nodes through the registry and reports every
    "orchestrator.*" name that ended up in `sys.modules` before and after.

None of orchestrator.lifecycle/research/knowledge or any lifecycle node
module exists yet (L0.1-L0.3 is foundation only, LIFECYCLE_design.md §10),
so both checks pass trivially today. They stay in CI as a guard against the
moment those packages do exist -- an isolation test is only useful written
ahead of the code it isolates against.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATOR_DIR = REPO_ROOT / "orchestrator"

# LIFECYCLE_design.md §2.1's package layout: everything the NN1 bounded
# research loop, or any lifecycle-only module, may live under.
FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "orchestrator.lifecycle",
    "orchestrator.research",
    "orchestrator.knowledge",
)

# The only orchestrator.nodes.* modules the fieldwork run_kind's own import
# graph may reach: fieldwork's own module, the shared NodeContext, the lazy
# registry itself, and the `orchestrator.nodes` package __init__.
ALLOWED_NODE_MODULES = {
    "orchestrator.nodes",
    "orchestrator.nodes.fieldwork",
    "orchestrator.nodes.context",
    "orchestrator.nodes.registry",
    # The fieldwork run's own `narrate` node (P6 WP N7): fieldwork, not a
    # lifecycle module.
    "orchestrator.nodes.narration",
}


def _module_path(module_name: str) -> Path | None:
    if module_name != "orchestrator" and not module_name.startswith("orchestrator."):
        return None
    parts = module_name.split(".")
    as_file = REPO_ROOT.joinpath(*parts).with_suffix(".py")
    if as_file.is_file():
        return as_file
    as_pkg = REPO_ROOT.joinpath(*parts, "__init__.py")
    if as_pkg.is_file():
        return as_pkg
    return None


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # Every import in orchestrator/ is absolute (level == 0, checked
            # separately by test_no_relative_imports_in_orchestrator below),
            # so a relative import here would be a real surprise, not
            # something this walk needs to resolve.
            if node.level == 0 and node.module:
                names.add(node.module)
                for alias in node.names:
                    # `from orchestrator.nodes import fieldwork` imports the
                    # submodule orchestrator.nodes.fieldwork, not merely an
                    # attribute of orchestrator.nodes -- add the fully
                    # qualified guess too; _module_path filters out whichever
                    # of the two doesn't correspond to a real file.
                    names.add(f"{node.module}.{alias.name}")
    return names


def _transitive_orchestrator_imports(entry_module: str) -> set[str]:
    # `resolved` is the answer (only names that correspond to a real file --
    # `from orchestrator.nodes.context import NodeContext`'s "guessed"
    # submodule name orchestrator.nodes.context.NodeContext must never end up
    # in it, since _module_path correctly finds no such file). `visited`
    # additionally tracks every name ever queued, real or not, purely to
    # avoid re-processing/looping -- it is never returned.
    resolved: set[str] = set()
    visited: set[str] = set()
    frontier = [entry_module]
    while frontier:
        name = frontier.pop()
        if name in visited:
            continue
        visited.add(name)
        path = _module_path(name)
        if path is None:
            continue
        resolved.add(name)
        for imported in _imported_names(path):
            if imported not in visited and (imported == "orchestrator" or imported.startswith("orchestrator.")):
                frontier.append(imported)
    return resolved


def test_no_relative_imports_in_orchestrator():
    # The AST walk above resolves only absolute imports (node.level == 0).
    # If a relative import ever appears under orchestrator/, the walk above
    # would silently under-count the import graph rather than fail loudly --
    # so this guards the walk's own assumption, not just a style rule.
    offenders = []
    for path in ORCHESTRATOR_DIR.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level and node.level > 0:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not offenders, f"relative imports found (breaks the isolation AST walk's assumption): {offenders}"


def test_fieldwork_import_graph_never_reaches_forbidden_packages():
    modules = _transitive_orchestrator_imports("orchestrator.nodes.fieldwork")

    forbidden = sorted(
        m for m in modules if any(m == prefix or m.startswith(prefix + ".") for prefix in FORBIDDEN_PREFIXES)
    )
    assert not forbidden, f"fieldwork's import graph reaches forbidden package(s): {forbidden}"

    node_modules = {m for m in modules if m == "orchestrator.nodes" or m.startswith("orchestrator.nodes.")}
    disallowed_node_modules = node_modules - ALLOWED_NODE_MODULES
    assert not disallowed_node_modules, (
        f"fieldwork's import graph reaches lifecycle node module(s): {disallowed_node_modules}"
    )


def test_registry_import_alone_is_lazy_and_resolving_fieldwork_stays_isolated(tmp_path):
    worker = REPO_ROOT / "tests" / "lifecycle_isolation_worker.py"
    out_path = tmp_path / "modules.json"
    proc = subprocess.run(
        [sys.executable, str(worker), str(out_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"worker failed:\n{proc.stdout}\n{proc.stderr}"
    data = json.loads(out_path.read_text())

    # Laziness: merely importing orchestrator.nodes.registry (which also
    # runs orchestrator/nodes/__init__.py, the package's own init) must not
    # import orchestrator.nodes.fieldwork.
    assert "orchestrator.nodes.fieldwork" not in data["before"], (
        "importing orchestrator.nodes.registry alone imported "
        "orchestrator.nodes.fieldwork -- resolution must be lazy, per run_kind"
    )
    for prefix in FORBIDDEN_PREFIXES:
        offenders = [m for m in data["before"] if m == prefix or m.startswith(prefix + ".")]
        assert not offenders, f"importing the registry alone imported forbidden module(s): {offenders}"

    # Resolving fieldwork's own nodes through the registry does import
    # fieldwork.py (proving the test actually exercised the real path)...
    assert "orchestrator.nodes.fieldwork" in data["after"]
    # ...but never a forbidden package, even transitively.
    for prefix in FORBIDDEN_PREFIXES:
        offenders = [m for m in data["after"] if m == prefix or m.startswith(prefix + ".")]
        assert not offenders, f"resolving fieldwork nodes imported forbidden module(s): {offenders}"


def test_registry_fieldwork_sequence_matches_fieldwork_nodes_for():
    # The registry is what the executor and service actually run; the
    # module-level NODES_FOR in nodes/fieldwork.py must never drift from it
    # (found 2026-09-24: `narrate` was added to one and not the other, so it
    # never ran through the real executor path).
    from orchestrator.nodes.fieldwork import NODES_FOR as FIELDWORK_NODES_FOR
    from orchestrator.nodes.registry import NODES_FOR as REGISTRY_NODES_FOR

    for phase, nodes in FIELDWORK_NODES_FOR["fieldwork"].items():
        registry_nodes = REGISTRY_NODES_FOR["fieldwork"][phase]
        assert [n for n, _ in registry_nodes] == [n for n, _ in nodes], phase
        assert [f for _, f in registry_nodes] == [f for _, f in nodes], phase
