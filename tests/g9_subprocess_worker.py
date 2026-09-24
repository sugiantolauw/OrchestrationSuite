"""Standalone worker for G9 cross-process determinism (CLAUDE.md §5 G9, P2/P3
gate review item 3). Run as a real subprocess (never imported by pytest) so
PYTHONHASHSEED genuinely differs between the two invocations -- an in-process
test shares one interpreter's hash seed for its whole run, which is exactly
the class of nondeterminism (set/dict iteration order, e.g.) this gate exists
to catch and an in-process comparison cannot.

Runs the real SKILL-001 T&E Skill's execute path (discover -> execute ->
find -> prioritise -> act -> export) against the small, fast
tests/fixtures/tne_planted/data/ fixture (the same fixture
tests/test_p3_tne_gates.py's other Tier A gates use, for the same reason: a
full synthetic_data/ pass takes minutes, this takes seconds -- see that
file's own module docstring), and writes every non-narrative output this run
produced to a JSON file at the given output path: run_metrics, flagged_rows
(sorted, since flagged_rows has no inherent scored order this run's own
determinism should be judged by), findings (with their rendered
observation/recommendation text, severities and exposure figures --
narrative fields are never populated at P2/P3, CLAUDE.md §3 NN2, so nothing
narrative to strip), management actions, and the XLSX workpaper's cell values
(openpyxl, sheet by sheet), with only the two declared generation-timestamp
footer cells excluded (the run's own `ctx.clock` is a fixed deterministic
stamp identical across both processes regardless, so this exclusion is belt
and braces, not load-bearing).

Usage: python tests/g9_subprocess_worker.py <db_path> <run_id> <output_json_path>
"""

from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests.conftest import canonical_ts  # noqa: E402

from orchestrator import runs as runs_module  # noqa: E402
from orchestrator.adapters.export_storage import LocalExportStorage  # noqa: E402
from orchestrator.adapters.persistence_local import LocalPersistence  # noqa: E402
from orchestrator.config import DEFAULT_PPTX_TEMPLATE_PATH  # noqa: E402
from orchestrator.contract import LocalFileDataSource  # noqa: E402
from orchestrator.nodes.context import NodeContext  # noqa: E402
from orchestrator.nodes.fieldwork import act, discover, execute, export, find, prioritise  # noqa: E402
from orchestrator.skills import load_skill  # noqa: E402

SKILL_DIR = REPO_ROOT / "skills" / "tne_exco"
DATA_DIR = REPO_ROOT / "tests" / "fixtures" / "tne_planted" / "data"
AUDIT_PERIOD = ("2025-01-01", "2026-04-30")

# The two footer cells every sheet stamps with a generation timestamp
# (orchestrator/nodes/fieldwork.py's `footer`, CLAUDE.md §9A.2) -- excluded
# by (sheet, row, col) so a genuine content divergence anywhere else on the
# same row can never hide behind this exclusion.
_FOOTER_MARKER = "Generated "


class _Settings:
    catalog = None
    schema = None
    pptx_template_path = DEFAULT_PPTX_TEMPLATE_PATH


def _fingerprint(fp_id: str, skill_content_hash: str | None) -> dict:
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


def _xlsx_cell_values(content: bytes) -> dict:
    import io

    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    out: dict[str, dict] = {}
    for sheet in wb.sheetnames:
        ws = wb[sheet]
        cells = {}
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                if isinstance(cell.value, str) and cell.value.startswith(_FOOTER_MARKER):
                    continue
                cells[cell.coordinate] = cell.value
        out[sheet] = cells
    return out


def main() -> None:
    db_path, run_id, output_path = sys.argv[1], sys.argv[2], sys.argv[3]

    persistence = LocalPersistence(db_path)
    persistence.migrate()

    skill = load_skill(SKILL_DIR)
    skill.validate()
    data_source = LocalFileDataSource(root_dir=DATA_DIR, sources=skill.contract["sources"])
    source_versions = {name: data_source.resolve_version(name) for name in skill.contract["sources"]}

    fp = _fingerprint(f"FP-{run_id}", skill.content_hash)
    now = canonical_ts(1)
    state = runs_module.create_run(
        persistence, run_id=run_id, run_kind="fieldwork", engagement_id="ENG-DEFAULT",
        skill_id=skill.skill_id, skill_version=skill.version, mode="playbook",
        audit_period=AUDIT_PERIOD, objective="G9 subprocess determinism", run_owner="gatebot",
        options={"auto_confirm_plan": True}, fingerprint=fp, now=now,
    )
    data_assets = [{"source": name, "table_fqn": name, "version": v} for name, v in source_versions.items()]
    state = persistence.save_state(dataclasses.replace(state, data_assets=data_assets))

    export_dir = Path(tempfile.mkdtemp(prefix="g9-subprocess-exports-"))
    ctx = NodeContext(
        settings=_Settings(), persistence=persistence, data_source=data_source,
        skill=skill, clock=lambda: canonical_ts(2), export_storage=LocalExportStorage(root_dir=export_dir),
    )

    state = discover(ctx, state)
    state = execute(ctx, state)
    state = find(ctx, state)
    state = prioritise(ctx, state)
    state = act(ctx, state)
    state = export(ctx, state)

    metrics = persistence.get_run_metrics(run_id)
    flagged = sorted(
        persistence.list_flagged_rows(run_id), key=lambda r: (r["source"], r["row_key"], r["flag"])
    )
    findings = persistence.list_findings(run_id)
    actions = persistence.list_management_actions(filters={"run_id": run_id})
    xlsx_path = state.exports["xlsx"]["path"]
    xlsx_content = ctx.export_storage.read(xlsx_path)

    result = {
        "metrics": metrics,
        "flagged_rows": flagged,
        "findings": [
            {k: v for k, v in f.items() if k not in ("created_at", "updated_at", "run_id")} for f in findings
        ],
        "management_actions": [
            {k: v for k, v in a.items() if k not in ("created_at", "updated_at", "run_id")} for a in actions
        ],
        "reconciliation": state.reconciliation,
        "xlsx_cells": _xlsx_cell_values(xlsx_content),
    }
    Path(output_path).write_text(json.dumps(result, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
