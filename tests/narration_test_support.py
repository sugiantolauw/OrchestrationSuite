"""Shared harness for the `narrate` node's own tests (P6 WP N7, tests
T-N1-T-N4, docs/specs/P6_narration_design.md §11). Builds the same "mini"
Skill fixture and tiny CSV data `tests/test_p3_nodes.py` uses (owned by the
P2 primitives work; not imported from there to avoid coupling two test
files' fixtures together), runs the pipeline through `prioritise`, and hands
back a harness ready to call `narrate(ctx, state)` -- or the rest of
`_run_full_execute_phase` for a full end-to-end pass including `act`.

`DispatchingModelClient` answers a `chat()` call by matching a MARKER
substring against the outgoing request (messages + params combined) rather
than by call order -- the `narrate` node's own internal call order (which
finding is narrated first, whether synthesis runs before or after priority)
is an implementation detail these tests should not have to track. Every
marker below is something that appears verbatim in exactly one task's
request: a wire schema's `schema_version` CONST value (embedded either in
an appended system message, when the resolved role's capabilities mark
`json_schema_strict` unsupported -- true for `model_sonnet` today, CLAUDE.md
§6 -- or in `params["response_format"]`, when it is supported), or, for the
two `find` calls that share the same schema_version, the finding's own
`finding_key` field as it appears verbatim in the compact-JSON payload
embedded in the user message."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pandas as pd

from orchestrator import runs as runs_module
from orchestrator.adapters.export_storage import LocalExportStorage
from orchestrator.adapters.protocols import ModelResponse
from orchestrator.config import Settings
from orchestrator.contract import LocalFileDataSource
from orchestrator.nodes.context import NodeContext
from orchestrator.nodes.fieldwork import act, classify, discover, execute, find, prioritise, profile
from orchestrator.skills import load_skill
from tests.conftest import canonical_ts

MINI_SKILL_DIR = Path(__file__).parent / "fixtures" / "skills" / "mini"
AUDIT_PERIOD = ("2026-01-01", "2026-02-28")
MODEL_SONNET_ENDPOINT = "ep-sonnet"
MODEL_GPT_OSS_ENDPOINT = "ep-gptoss"


def _fingerprint(fp_id: str, skill_content_hash: str | None = None) -> dict:
    return dict(
        fingerprint_id=fp_id, source_table_versions="{}", uploaded_file_hashes="{}",
        reference_data_hashes="{}", skill_content_hash=skill_content_hash, code_revision="rev1",
        dependency_lock_hash="dep1", runtime_config_hash="rc1", endpoint_config="{}",
        prompt_template_version="none", created_at=canonical_ts(0),
    )


def _write_mini_data(root: Path) -> None:
    claims = pd.DataFrame(
        [
            {"Employee ID": 1, "Transaction Date": "2026-01-05", "Amount": 100, "Vendor": "VendorA"},
            {"Employee ID": 1, "Transaction Date": "2026-01-06", "Amount": 600, "Vendor": "VendorA"},
            {"Employee ID": 2, "Transaction Date": "2026-01-10", "Amount": 700, "Vendor": "VendorB"},
            {"Employee ID": 3, "Transaction Date": "2026-01-15", "Amount": 50, "Vendor": "VendorC"},
            {"Employee ID": 4, "Transaction Date": "2026-02-01", "Amount": 900, "Vendor": "VendorD"},
        ]
    )
    register = pd.DataFrame(
        [
            {"Employee ID": 1, "Transaction Date": "2026-01-05", "Vendor": "VendorA"},
            {"Employee ID": 2, "Transaction Date": "2026-01-10", "Vendor": "VendorB"},
            {"Employee ID": 4, "Transaction Date": "2026-02-01", "Vendor": "VendorD"},
        ]
    )
    claims.to_csv(root / "claims.csv", index=False)
    register.to_csv(root / "register.csv", index=False)


@dataclasses.dataclass
class NarrationHarness:
    ctx: NodeContext
    state: object
    persistence: object


def make_narration_harness(
    local_persistence, tmp_path, *, model_client=None, narration_enabled: bool = True,
    model_sonnet: str | None = MODEL_SONNET_ENDPOINT, model_gpt_oss: str | None = MODEL_GPT_OSS_ENDPOINT,
    llm_cache_mode: str = "live", run_owner: str = "alice", engagement_id: str = "ENG-DEFAULT",
    skill_dir: Path = MINI_SKILL_DIR, ai_proposed_findings_enabled: bool = False,
    narration_max_candidates: int = 3, data_writer=_write_mini_data, narration_max_parallel: int = 2,
    llm_retry_backoff_s: float = 0.0, llm_max_transport_attempts: int = 3,
) -> NarrationHarness:
    # `skill_dir`/`ai_proposed_findings_enabled`/`narration_max_candidates`/
    # `data_writer` (P6 WP N8): every existing caller omits them, so every
    # existing test's behaviour is unchanged -- `tests/test_candidates.py`
    # is the only caller that passes a non-default `skill_dir` (a dedicated
    # fixture with a genuine C-2 anchor; the shared `mini` Skill's two rule
    # findings fully cite both of their own tests' metrics, so it has none)
    # or a non-default `data_writer` (G10: a clean population, no
    # exceptions on either test).
    persistence = local_persistence
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    data_writer(data_dir)

    skill = load_skill(skill_dir)
    skill.validate()
    data_source = LocalFileDataSource(root_dir=data_dir, sources=skill.contract["sources"])
    source_versions = {name: data_source.resolve_version(name) for name in skill.contract["sources"]}

    fp = _fingerprint(f"FP-{tmp_path.name}", skill.content_hash)
    now = canonical_ts(1)
    state = runs_module.create_run(
        persistence, run_kind="fieldwork", engagement_id=engagement_id, skill_id=skill.skill_id,
        skill_version=skill.version, mode="playbook", audit_period=AUDIT_PERIOD, objective="t",
        run_owner=run_owner, options={"auto_confirm_plan": True}, fingerprint=fp, now=now,
    )
    data_assets = [{"source": name, "table_fqn": name, "version": version} for name, version in source_versions.items()]
    state = persistence.save_state(dataclasses.replace(state, data_assets=data_assets))

    settings = Settings(
        model_sonnet=model_sonnet, model_gpt_oss=model_gpt_oss, narration_enabled=narration_enabled,
        llm_cache_mode=llm_cache_mode, ai_proposed_findings_enabled=ai_proposed_findings_enabled,
        narration_max_candidates=narration_max_candidates, narration_max_parallel=narration_max_parallel,
        # Test-suite default 0.0 (production default is 5.0, orchestrator.config):
        # no existing narration test exercises a RateLimited/TransientModelError
        # retry, so a fast default keeps the harness quick without needing every
        # caller to pass it; a test that DOES exercise retries overrides this.
        llm_retry_backoff_s=llm_retry_backoff_s, llm_max_transport_attempts=llm_max_transport_attempts,
    )
    export_dir = tmp_path / "exports"
    ctx = NodeContext(
        settings=settings, persistence=persistence, data_source=data_source, skill=skill,
        clock=lambda: canonical_ts(2), export_storage=LocalExportStorage(root_dir=export_dir),
        model_client=model_client,
    )
    return NarrationHarness(ctx=ctx, state=state, persistence=persistence)


def run_to_narrate_input(h: NarrationHarness):
    """Runs discover -> profile -> execute -> classify -> find -> prioritise,
    the same state `narrate` receives in the real pipeline (CLAUDE.md §4.2's
    amended fieldwork order, §2 of the design)."""
    state = discover(h.ctx, h.state)
    state = profile(h.ctx, state)
    state = execute(h.ctx, state)
    state = classify(h.ctx, state)
    state = find(h.ctx, state)
    state = prioritise(h.ctx, state)
    return state


def run_narrate_then_act(h: NarrationHarness, narrate_fn):
    state = run_to_narrate_input(h)
    state = narrate_fn(h.ctx, state)
    state = act(h.ctx, state)
    return state


class DispatchingModelClient:
    """See the module docstring. `by_marker` maps a marker substring -> a
    `ModelResponse`/`Exception`, or a list of either consumed in call order
    for that marker (scripting "invalid once, then valid" repair scenarios).
    `default`, if set, answers any call matching no marker; otherwise an
    unmatched call raises loudly (a test with a genuine gap in its response
    map should fail, not silently return nothing)."""

    def __init__(self, by_marker: dict[str, Any], *, default: Any = None):
        self.by_marker = by_marker
        self.default = default
        self.calls: list[dict] = []

    def chat(self, *, endpoint: str, messages: list[dict], params: dict, timeout_s: float) -> ModelResponse:
        self.calls.append({"endpoint": endpoint, "messages": messages, "params": params})
        haystack = str(messages) + str(params)
        for marker, entry in self.by_marker.items():
            if marker in haystack:
                return self._resolve(entry, marker)
        if self.default is not None:
            return self._resolve(self.default, "<default>")
        raise AssertionError(f"DispatchingModelClient: no marker matched for endpoint {endpoint!r}: {messages!r}")

    @staticmethod
    def _resolve(entry: Any, marker: str) -> ModelResponse:
        if isinstance(entry, list):
            if not entry:
                raise AssertionError(f"DispatchingModelClient: responses exhausted for marker {marker!r}")
            item = entry.pop(0)
        else:
            item = entry
        if isinstance(item, Exception):
            raise item
        return item

    def describe_endpoint(self, endpoint: str) -> dict:
        return {"foundation_model": None, "ready": True}


def resp(payload: dict, *, model: str = "test-model-v1", reasoning: int = 0) -> ModelResponse:
    # Compact separators (no spaces), matching orchestrator.narration.
    # payloads/runner's own `_canonical_json` -- so a repair round's "Your
    # previous output" text (built from THIS response's own `.text`) keeps
    # markers like MARKER_FIND_T1 matching in the same compact form they
    # match in a first-attempt schema/payload embedding.
    return ModelResponse(
        text=json.dumps(payload, separators=(",", ":")), served_model_version=model, finish_reason="stop",
        prompt_tokens=10, completion_tokens=5, total_tokens=15, request_id="req-1",
        reasoning_parts_stripped=reasoning, latency_ms=10,
    )


# Markers -- kept as constants so a test that only wants to override ONE
# response can copy `happy_responses()` and replace a single key.
MARKER_FIND_T1 = '"finding_key":"T1"'
MARKER_FIND_T2 = '"finding_key":"T2"'
MARKER_SYNTHESIS = "finding-synthesis/1"
MARKER_PRIORITY = "priority-rationale/1"
MARKER_REMEDIATION = "remediation/1"
MARKER_EXEC_SUMMARY = "exec-summary/1"
MARKER_CAPTIONS = "chart-captions/1"
MARKER_PROFILE = "profile-narrative/1"


def happy_responses(*, model: str = "test-model-v1") -> dict[str, ModelResponse]:
    """A fully valid (§3.3-§3.4-clean) response for every narration call the
    mini Skill fixture's `narrate` execution makes: two `find` calls (T1
    High, T2 Low), one `find_synthesis`, one `prioritise`, one `act`, one
    `export_summary` and one `export_caption` call, plus `profile`. Every
    piece of prose here uses only typed placeholders for quantities --
    never a literal digit -- exactly the discipline the validator itself
    enforces on real model output."""
    return {
        MARKER_FIND_T1: resp(
            {
                "schema_version": "finding-narration/1", "finding_key": "T1",
                "observation": (
                    "This run found {count:hv_count} high-value claim(s), totalling {money:hv_amount}."
                ),
                "recommendation": "Review high-value claims for legitimacy before reimbursement.",
                "management_questions": ["What review step currently catches a claim like this before payment?"],
            },
            model=model,
        ),
        MARKER_FIND_T2: resp(
            {
                "schema_version": "finding-narration/1", "finding_key": "T2",
                "observation": (
                    "This run found {count:missing_count} claim(s) with no matching register entry, "
                    "{pct:missing_pct} of the tested population, totalling {money:missing_amount}."
                ),
                "recommendation": "Reconcile claims against the booking register before payment.",
                "management_questions": ["What causes a claim to be missing its register match?"],
            },
            model=model,
        ),
        MARKER_SYNTHESIS: resp(
            {
                "schema_version": "finding-synthesis/1",
                "themes": [
                    {
                        "title": "Expense review gaps",
                        "summary": "Both matters concern gaps in reviewing claims before they are reimbursed.",
                        "root_cause_hypothesis": (
                            "This pattern may reflect a review step that runs after payment rather than before it."
                        ),
                        "finding_keys": ["T1", "T2"],
                        "review_observations": [],
                    }
                ],
                "severity_proposals": [],
            },
            model=model,
        ),
        MARKER_PRIORITY: resp(
            {
                "schema_version": "priority-rationale/1",
                "items": [
                    {"key": "T1", "rationale": "This matter carries a High severity rating in this run."},
                    {"key": "T2", "rationale": "This matter carries a Low severity rating in this run."},
                ],
            },
            model=model,
        ),
        MARKER_REMEDIATION: resp(
            {
                "schema_version": "remediation/1",
                "items": [
                    {
                        "key": "T1",
                        "remediation": (
                            "The travel and expense team should tighten review of high-value claims "
                            "before reimbursement."
                        ),
                    },
                    {
                        "key": "T2",
                        "remediation": (
                            "The travel and expense team should reconcile claims against the booking "
                            "register before payment."
                        ),
                    },
                ],
            },
            model=model,
        ),
        MARKER_EXEC_SUMMARY: resp(
            {
                "schema_version": "exec-summary/1",
                "paragraphs": [
                    "This run raised {count:run_finding_count} finding(s) across the tested population.",
                    # Round-5 narration-content review, item 2 (N-C1 on
                    # exec_summary): this mini Skill's own two findings both
                    # carry a real "spend" monetary_basis, so `run_values`
                    # always resolves a real dominant contributor here --
                    # the coverage rule now requires citing it by name and
                    # amount, not only the headline total.
                    "The amount at risk this run identified totals {money:run_exposure_headline}, "
                    "driven mainly by {value:run_exposure_dominant_title}, which accounts for "
                    "{money:run_exposure_dominant_amount} of that total.",
                ],
            },
            model=model,
        ),
        MARKER_CAPTIONS: resp(
            {
                "schema_version": "chart-captions/1",
                "captions": [
                    {
                        "chart_id": "severity_distribution",
                        "caption": "This chart shows the count of findings at each severity level for this run.",
                    },
                    {
                        "chart_id": "risk_and_exposure",
                        "caption": "This chart shows the amount at risk identified by this run.",
                    },
                ],
            },
            model=model,
        ),
        MARKER_PROFILE: resp(
            {
                "schema_version": "profile-narrative/1",
                "paragraphs": ["Source data completeness was reviewed for the sources used in this run."],
            },
            model=model,
        ),
    }
