"""Endpoint parameter probe (CLAUDE.md §6 "Per-endpoint parameter support
must be tested and recorded, not assumed"; referenced by
docs/specs/P6_P8_explorer_llm_design.md §1 and the header comment of
orchestrator/llm/capabilities.yaml, which named this script before it
existed). Sends one bare chat completion per configured role, then one
further completion per candidate parameter (in isolation, never combined),
and records pass/fail for each into a JSON report.

This is the empirical exercise the recorded matrix in
orchestrator/llm/capabilities.yaml came from (docs/specs/
P6_P8_explorer_llm_design.md §1, "Recorded parameter matrix"). It is a
narrower companion to tests/live/test_llm_endpoints_live.py, which only
makes one bare completion per endpoint to confirm it answers at all; this
script goes on to probe each parameter this codebase might ever send
(orchestrator/llm/tasks.py's TASK_PROFILES, orchestrator/llm/
capabilities.yaml's own candidate list).

It NEVER writes orchestrator/llm/capabilities.yaml itself: a human reads the
report and updates the matrix deliberately (the same rule
test_llm_endpoints_live.py's docstring states). It never runs against a
live endpoint from an agent session (CLAUDE.md operating instructions) --
its logic is exercised by tests/test_probe_endpoint_params.py against a
fake client, never live here.

Usage:
    python scripts/probe_endpoint_params.py [--out DIR] [--timeout-s N]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PACKAGE_ROOT / ".env")

from orchestrator.config import Settings, load_settings  # noqa: E402
from orchestrator.llm.errors import (  # noqa: E402
    LLMConfigError,
    ModelUnavailable,
    RateLimited,
    TransientModelError,
    TruncatedOutput,
)

# The default roles a "known nothing" probe checks. A caller with a
# narrower or wider role set (e.g. a future MODEL_RESEARCH role,
# LIFECYCLE_design.md §2) passes `roles=` to run_probe() directly.
DEFAULT_ROLES: tuple[str, ...] = ("model_sonnet", "model_gpt_oss")

_BARE_MESSAGES = [{"role": "user", "content": "Reply with the single word: ok"}]

_PROBE_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}

# One chat-completion parameter per probe, in isolation -- never combined,
# so a rejection is attributable to exactly one parameter. This is the same
# candidate set orchestrator/llm/capabilities.yaml records support for
# (§3.3), plus json_object and thinking, which the matrix also names.
CANDIDATE_PARAMS: dict[str, dict[str, Any]] = {
    "max_tokens": {"max_tokens": 16},
    "temperature": {"temperature": 0},
    "top_p": {"top_p": 1},
    "reasoning_effort": {"reasoning_effort": "low"},
    "json_schema_strict": {
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "probe", "strict": True, "schema": _PROBE_SCHEMA},
        },
    },
    "json_object": {"response_format": {"type": "json_object"}},
    "seed": {"seed": 1},
    "stop": {"stop": ["END"]},
    "max_completion_tokens": {"max_completion_tokens": 16},
    "thinking": {"thinking": {"type": "enabled"}},
}


@dataclass(frozen=True)
class ParamProbeResult:
    param: str
    outcome: str  # "supported" | "rejected" | "unavailable" | "transient_error" | "truncated" | "error"
    reason: str | None
    served_model_version: str | None
    finish_reason: str | None


@dataclass(frozen=True)
class RoleProbeResult:
    role: str
    endpoint: str | None
    baseline: ParamProbeResult
    params: list[ParamProbeResult] = field(default_factory=list)


@dataclass(frozen=True)
class ProbeReport:
    generated_at: str
    roles: list[RoleProbeResult]


def _probe_call(client, *, endpoint: str, param_name: str, params: dict, timeout_s: float) -> ParamProbeResult:
    """One isolated chat completion. Every failure mode is caught and
    recorded as a result, never raised -- one bad/unsupported parameter
    must never abort the rest of the probe (this is exactly the "never a
    fake successful integration, never a silent crash either" rule, NN13,
    applied to a diagnostic script rather than a node)."""
    try:
        resp = client.chat(endpoint=endpoint, messages=_BARE_MESSAGES, params=params, timeout_s=timeout_s)
    except ModelUnavailable as exc:
        return ParamProbeResult(param_name, "unavailable", str(exc), None, None)
    except (RateLimited, TransientModelError) as exc:
        return ParamProbeResult(param_name, "transient_error", str(exc), None, None)
    except LLMConfigError as exc:
        # A 400 -- the endpoint understood and rejected this exact
        # parameter. This is the interesting "rejected" case the matrix
        # exists to record.
        return ParamProbeResult(param_name, "rejected", str(exc), None, None)
    except TruncatedOutput as exc:
        return ParamProbeResult(param_name, "truncated", str(exc), None, "length")
    except Exception as exc:  # noqa: BLE001 -- reclassified as "error", never left to crash the probe
        return ParamProbeResult(param_name, "error", f"{type(exc).__name__}: {exc}", None, None)
    return ParamProbeResult(param_name, "supported", None, resp.served_model_version, resp.finish_reason)


def probe_role(client, *, role: str, endpoint: str | None, timeout_s: float) -> RoleProbeResult:
    if not endpoint:
        return RoleProbeResult(
            role=role, endpoint=None,
            baseline=ParamProbeResult("(bare)", "unconfigured", "no endpoint configured for this role", None, None),
        )
    baseline = _probe_call(client, endpoint=endpoint, param_name="(bare)", params={}, timeout_s=timeout_s)
    if baseline.outcome in ("unavailable", "transient_error"):
        # The endpoint is not answering at all -- probing ten more
        # parameters against it would only multiply failed calls for no
        # new information (CLAUDE.md §11 cost incident: never multiply
        # calls against a known-dead endpoint).
        return RoleProbeResult(role=role, endpoint=endpoint, baseline=baseline)
    params = [
        _probe_call(client, endpoint=endpoint, param_name=name, params=param_dict, timeout_s=timeout_s)
        for name, param_dict in CANDIDATE_PARAMS.items()
    ]
    return RoleProbeResult(role=role, endpoint=endpoint, baseline=baseline, params=params)


def run_probe(
    client, settings: Settings, *, roles: tuple[str, ...] = DEFAULT_ROLES,
    timeout_s: float | None = None, clock=lambda: datetime.now(timezone.utc).isoformat(),
) -> ProbeReport:
    """Pure orchestration over an injected `client` -- never constructs its
    own ModelClient -- so it is exercised identically against
    FakeModelClient in tests and DatabricksModelClient from main()."""
    effective_timeout = timeout_s if timeout_s is not None else getattr(settings, "llm_timeout_s", 30.0)
    role_results = [
        probe_role(client, role=role, endpoint=getattr(settings, role, None), timeout_s=effective_timeout)
        for role in roles
    ]
    return ProbeReport(generated_at=clock(), roles=role_results)


def report_to_dict(report: ProbeReport) -> dict:
    return {
        "generated_at": report.generated_at,
        "roles": [
            {
                "role": r.role, "endpoint": r.endpoint,
                "baseline": asdict(r.baseline),
                "params": [asdict(p) for p in r.params],
            }
            for r in report.roles
        ],
    }


def write_report(report: ProbeReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report_to_dict(report), indent=2, sort_keys=True) + "\n")


def print_report(report: ProbeReport) -> None:
    print(f"Endpoint parameter probe -- {report.generated_at}")
    for r in report.roles:
        if not r.endpoint:
            print(f"  {r.role}: no endpoint configured, skipping")
            continue
        print(f"  {r.role} ({r.endpoint}): baseline={r.baseline.outcome}"
              + (f" ({r.baseline.reason})" if r.baseline.reason else ""))
        for p in r.params:
            line = f"    {p.param:24s} {p.outcome}"
            if p.reason:
                line += f" -- {p.reason}"
            print(line)
    print(
        "This report is NOT applied automatically. A human reviews it and updates "
        "orchestrator/llm/capabilities.yaml deliberately (CLAUDE.md §6)."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", default=str(PACKAGE_ROOT / ".local" / "endpoint_probe"),
        help="output directory for the JSON report (default: .local/endpoint_probe)",
    )
    parser.add_argument(
        "--timeout-s", type=float, default=None,
        help="per-call timeout override (default: Settings.llm_timeout_s)",
    )
    return parser


def main(argv: list[str] | None = None, *, client=None, settings: Settings | None = None) -> int:
    """Importable entry point (same pattern as scripts/check_idle_cost.py
    and scripts/run_surface1.py). `client`/`settings` are injectable so
    tests never construct a real DatabricksModelClient or read the real
    .env -- the only path that does is the `if __name__` block below."""
    args = build_parser().parse_args(argv)
    settings = settings if settings is not None else load_settings()
    if client is None:
        from orchestrator.adapters.model_databricks import DatabricksModelClient

        client = DatabricksModelClient()

    report = run_probe(client, settings, timeout_s=args.timeout_s)

    out_dir = Path(args.out)
    stamp = report.generated_at.replace(":", "").replace("-", "").replace(".", "_")
    json_path = out_dir / f"endpoint_probe_{stamp}.json"
    write_report(report, json_path)

    print_report(report)
    print(f"JSON: {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
