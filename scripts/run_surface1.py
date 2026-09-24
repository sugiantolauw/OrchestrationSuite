"""Surface 1 CLI (CLAUDE.md §5 Tier B, §11 "Surface 1 harness"): scores a
COMPLETED run against an auditor-confirmed oracle file and writes a JSON and
an XLSX report. At the corporate workspace this is the only thing that needs
to run -- the oracle file (never committed to this repo, CLAUDE.md §9) is
dropped in and pointed at with `--oracle`; no code here changes.

Usage:
    python scripts/run_surface1.py --run-id <run_id> --oracle PATH --out DIR
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PACKAGE_ROOT / ".env")

from orchestrator.errors import RunNotFound  # noqa: E402
from orchestrator.eval.surface1 import (  # noqa: E402
    Surface1OracleError,
    Surface1RunError,
    run_surface1,
    write_json_report,
    write_xlsx_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, help="a completed (or awaiting_signoff) run_id")
    parser.add_argument("--oracle", required=True, help="path to the auditor-confirmed oracle file (.csv or .xlsx)")
    parser.add_argument(
        "--out", default=str(PACKAGE_ROOT / ".local" / "surface1"),
        help="output directory for the JSON and XLSX reports (default: .local/surface1)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Importable entry point (same pattern as scripts/check_idle_cost.py --
    runnable from ops/notebooks/run_surface1.py, not only the command line).
    Returns the process exit code rather than calling sys.exit itself."""
    args = build_parser().parse_args(argv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        report = run_surface1(args.run_id, args.oracle)
    except (Surface1OracleError, Surface1RunError, RunNotFound) as exc:
        print(f"Surface 1 FAILED: {exc}", file=sys.stderr)
        return 1

    json_path = out_dir / f"surface1_{args.run_id}.json"
    xlsx_path = out_dir / f"surface1_{args.run_id}.xlsx"
    write_json_report(report, json_path)
    write_xlsx_report(report, xlsx_path)

    print(f"Surface 1 report for run {args.run_id!r}:")
    for test_id, score in sorted(report.tests.items()):
        if score.status == "scored":
            print(
                f"  {test_id:8s} scored      TP={score.true_positives} FP={score.false_positives} "
                f"FN={score.false_negatives} precision={score.precision} recall={score.recall}"
            )
        else:
            print(f"  {test_id:8s} {score.status}" + (f" ({score.reason})" if score.reason else ""))
    n_metric_mismatches = sum(1 for m in report.metrics if not m.match)
    print(f"  metrics: {len(report.metrics)} compared, {n_metric_mismatches} mismatch(es)")
    print(f"JSON: {json_path}")
    print(f"XLSX: {xlsx_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
