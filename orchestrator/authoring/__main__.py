"""`python -m orchestrator.authoring {scaffold|validate|generate-fixtures|score}`
(docs/specs/P7_mapping_authoring_design.md §2.2 "CLI"). Every subcommand
also runs, cell-for-cell, from `ops/notebooks/skill_authoring.py` on a
workspace cluster (CLAUDE.md §11 "Operations must run from a workspace
cluster/notebook") -- this module is the one implementation both call."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from orchestrator.authoring.checks import validate_skill_dir
from orchestrator.authoring.scaffold import SampleSpec, scaffold_skill


def _parse_sample(spec: str) -> tuple[str, SampleSpec]:
    if "=" not in spec:
        raise argparse.ArgumentTypeError(f"--sample must be NAME=PATH, got {spec!r}")
    name, path = spec.split("=", 1)
    return name, SampleSpec(path=path)


def _cmd_scaffold(args: argparse.Namespace) -> int:
    sources = dict(args.sample) if args.sample else None
    dest = scaffold_skill(
        args.dest,
        skill_id=args.skill_id,
        name=args.name,
        domain=args.domain,
        owner=args.owner,
        timezone=args.timezone,
        sources=sources,
    )
    print(f"Scaffolded Skill {args.skill_id!r} at {dest}")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    report = validate_skill_dir(args.skill_dir, fixtures=args.fixtures, plants=args.plants)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(report.render_table())
        print()
        print("PASSED" if report.ok else f"FAILED ({len(report.errors)} error(s), {len(report.warnings)} warning(s))")
    return 0 if report.ok else 1


def _cmd_generate_fixtures(args: argparse.Namespace) -> int:
    # Lazy import: fixtures.py is a B2b file (§4 batch table), imported
    # only when this subcommand actually runs.
    from orchestrator.authoring.fixtures import generate_fixtures

    written = generate_fixtures(args.skill_dir, args.plants, args.out)
    payload = {source: len(rows) for source, rows in written.items()}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Generated fixture data under {Path(args.out)}:")
        for source, n in sorted(payload.items()):
            print(f"  {source}: {n} row(s)")
    return 0


def _cmd_score(args: argparse.Namespace) -> int:
    from orchestrator.authoring.score import score_fixtures

    scores = score_fixtures(args.skill_dir, args.plants, args.data)
    if args.json:
        print(json.dumps({tid: s.to_dict() for tid, s in scores.items()}, indent=2, sort_keys=True))
        return 0
    ok = True
    for test_id, s in sorted(scores.items()):
        passed = (s.precision is None or s.precision >= 0.98) and (s.recall is None or s.recall >= 0.95)
        ok = ok and passed
        print(
            f"{test_id:16s} plants={s.n_plants:3d} negs={s.n_negatives:3d} "
            f"TP={s.true_positives:3d} FP={s.false_positives:2d} FN={s.false_negatives:2d} "
            f"precision={s.precision} recall={s.recall}"
        )
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m orchestrator.authoring")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scaffold = sub.add_parser("scaffold", help="write a new, empty-but-valid Skill directory")
    p_scaffold.add_argument("dest")
    p_scaffold.add_argument("--skill-id", required=True)
    p_scaffold.add_argument("--name", required=True)
    p_scaffold.add_argument("--domain", required=True)
    p_scaffold.add_argument("--owner", required=True)
    p_scaffold.add_argument("--timezone", required=True)
    p_scaffold.add_argument("--sample", action="append", type=_parse_sample, default=[], metavar="NAME=PATH")
    p_scaffold.set_defaults(func=_cmd_scaffold)

    p_validate = sub.add_parser("validate", help="collect-all validation, optionally with a dry-run Surface 2 pass")
    p_validate.add_argument("skill_dir")
    p_validate.add_argument("--fixtures", help="directory of generated fixture data")
    p_validate.add_argument("--plants", help="path to the sidecar plants.yaml oracle")
    p_validate.add_argument("--json", action="store_true")
    p_validate.set_defaults(func=_cmd_validate)

    p_gen = sub.add_parser("generate-fixtures", help="generate planted-exception fixture data from plants.yaml")
    p_gen.add_argument("skill_dir")
    p_gen.add_argument("--plants", required=True)
    p_gen.add_argument("--out", required=True)
    p_gen.add_argument("--json", action="store_true")
    p_gen.set_defaults(func=_cmd_generate_fixtures)

    p_score = sub.add_parser("score", help="score already-generated fixture data against plants.yaml")
    p_score.add_argument("skill_dir")
    p_score.add_argument("--plants", required=True)
    p_score.add_argument("--data", required=True)
    p_score.add_argument("--json", action="store_true")
    p_score.set_defaults(func=_cmd_score)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 -- CLI top level: report, never traceback the user
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
