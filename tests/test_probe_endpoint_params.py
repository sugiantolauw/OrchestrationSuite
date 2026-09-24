"""scripts/probe_endpoint_params.py (WP N6): sends one bare completion per
role and one further completion per candidate parameter, records pass/fail,
and never writes orchestrator/llm/capabilities.yaml. Exercised entirely
against FakeModelClient / small local fakes -- no network, ever."""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.adapters.model_fake import FakeModelClient
from orchestrator.adapters.protocols import ModelResponse
from orchestrator.config import Settings
from orchestrator.llm.errors import LLMConfigError, ModelUnavailable, TransientModelError

import scripts.probe_endpoint_params as probe


def _resp(model="gpt-oss-120b-080525", finish_reason="stop"):
    return ModelResponse(
        text="ok", served_model_version=model, finish_reason=finish_reason,
        prompt_tokens=5, completion_tokens=1, total_tokens=6, request_id="req-1",
        reasoning_parts_stripped=0, latency_ms=10,
    )


def _settings(**overrides):
    base = dict(model_sonnet="databricks-claude-sonnet-5", model_gpt_oss="databricks-gpt-oss-120b")
    base.update(overrides)
    return Settings(**base)


# ── run_probe: the core, injectable orchestration ────────────────────────


def test_every_candidate_param_is_probed_in_isolation_and_recorded_supported():
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": _resp()})
    settings = _settings(model_sonnet=None)
    report = probe.run_probe(client, settings, roles=("model_sonnet", "model_gpt_oss"))

    gpt_oss = next(r for r in report.roles if r.role == "model_gpt_oss")
    assert gpt_oss.baseline.outcome == "supported"
    probed_names = {p.param for p in gpt_oss.params}
    assert probed_names == set(probe.CANDIDATE_PARAMS)
    assert all(p.outcome == "supported" for p in gpt_oss.params)
    # baseline (bare) + one call per candidate param, never combined
    assert len(client.calls) == 1 + len(probe.CANDIDATE_PARAMS)
    # each call sent exactly one candidate's params (or none, for the baseline)
    sent_param_sets = [c["params"] for c in client.calls]
    assert {} in sent_param_sets
    for name, expected in probe.CANDIDATE_PARAMS.items():
        assert expected in sent_param_sets


def test_unconfigured_role_is_skipped_without_any_call():
    client = FakeModelClient()
    settings = _settings(model_sonnet=None)
    report = probe.run_probe(client, settings, roles=("model_sonnet",))
    sonnet = report.roles[0]
    assert sonnet.endpoint is None
    assert sonnet.baseline.outcome == "unconfigured"
    assert sonnet.params == []
    assert client.calls == []


def test_unavailable_baseline_skips_every_further_param_probe():
    """CLAUDE.md §11 cost incident: a dead endpoint is not probed ten more
    times for no new information."""
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": ModelUnavailable("databricks-gpt-oss-120b", "rate limit of 0"),
    })
    settings = _settings(model_sonnet=None)
    report = probe.run_probe(client, settings, roles=("model_gpt_oss",))
    gpt_oss = report.roles[0]
    assert gpt_oss.baseline.outcome == "unavailable"
    assert gpt_oss.params == []
    assert len(client.calls) == 1


def test_transient_error_baseline_also_skips_further_probes():
    client = FakeModelClient(responses={
        "databricks-gpt-oss-120b": TransientModelError("databricks-gpt-oss-120b", "boom"),
    })
    settings = _settings(model_sonnet=None)
    report = probe.run_probe(client, settings, roles=("model_gpt_oss",))
    gpt_oss = report.roles[0]
    assert gpt_oss.baseline.outcome == "transient_error"
    assert gpt_oss.params == []


class _SelectiveClient:
    """A fake whose answer depends on which candidate parameter was sent,
    so a probe can exercise a rejection for exactly one parameter without
    aborting the rest -- FakeModelClient's endpoint-keyed responses can't
    express that."""

    def __init__(self, *, rejects: set[str]):
        self._rejects = rejects
        self.calls: list[dict] = []

    def chat(self, *, endpoint, messages, params, timeout_s):
        self.calls.append({"endpoint": endpoint, "params": params})
        if self._rejects & set(params):
            raise LLMConfigError(f"model endpoint {endpoint!r} rejected the request (400): bad param")
        return _resp()

    def describe_endpoint(self, endpoint):
        return {"foundation_model": None, "ready": True}


def test_one_rejected_param_does_not_abort_the_rest():
    client = _SelectiveClient(rejects={"seed"})
    settings = _settings(model_sonnet=None)
    report = probe.run_probe(client, settings, roles=("model_gpt_oss",))
    gpt_oss = report.roles[0]
    by_name = {p.param: p for p in gpt_oss.params}
    assert by_name["seed"].outcome == "rejected"
    assert by_name["seed"].reason and "bad param" in by_name["seed"].reason
    assert by_name["max_tokens"].outcome == "supported"
    # every OTHER candidate was still probed
    assert len(gpt_oss.params) == len(probe.CANDIDATE_PARAMS)


def test_unexpected_exception_is_recorded_as_error_not_raised():
    class _RaisingClient:
        def chat(self, **kwargs):
            raise RuntimeError("network exploded")

        def describe_endpoint(self, endpoint):
            return {"ready": False}

    settings = _settings(model_sonnet=None)
    report = probe.run_probe(_RaisingClient(), settings, roles=("model_gpt_oss",))
    gpt_oss = report.roles[0]
    assert gpt_oss.baseline.outcome == "error"
    assert "RuntimeError" in gpt_oss.baseline.reason


# ── report shape and file I/O ────────────────────────────────────────────


def test_report_to_dict_round_trips_through_json(tmp_path):
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": _resp()})
    settings = _settings(model_sonnet=None)
    report = probe.run_probe(client, settings, roles=("model_gpt_oss",))
    path = tmp_path / "report.json"
    probe.write_report(report, path)
    loaded = json.loads(path.read_text())
    assert loaded["generated_at"] == report.generated_at
    assert loaded["roles"][0]["role"] == "model_gpt_oss"
    assert loaded["roles"][0]["baseline"]["outcome"] == "supported"
    assert {p["param"] for p in loaded["roles"][0]["params"]} == set(probe.CANDIDATE_PARAMS)


# ── main(argv): the importable CLI entry point ───────────────────────────


def test_main_writes_a_json_report_and_returns_0(tmp_path):
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": _resp()})
    settings = _settings(model_sonnet=None)
    exit_code = probe.main(["--out", str(tmp_path)], client=client, settings=settings)
    assert exit_code == 0
    written = list(tmp_path.glob("endpoint_probe_*.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text())
    by_role = {r["role"]: r for r in payload["roles"]}
    assert by_role["model_gpt_oss"]["baseline"]["outcome"] == "supported"
    assert by_role["model_sonnet"]["baseline"]["outcome"] == "unconfigured"


def test_main_never_writes_capabilities_yaml(tmp_path):
    capabilities_path = Path(probe.__file__).resolve().parent.parent / "orchestrator" / "llm" / "capabilities.yaml"
    before = capabilities_path.read_bytes()
    client = FakeModelClient(responses={"databricks-gpt-oss-120b": _resp()})
    settings = _settings(model_sonnet=None)
    probe.main(["--out", str(tmp_path)], client=client, settings=settings)
    after = capabilities_path.read_bytes()
    assert before == after
    assert not (tmp_path / "capabilities.yaml").exists()


def test_main_respects_timeout_override(tmp_path):
    seen_timeouts: list[float] = []

    class _TimeoutSpyClient:
        def chat(self, *, endpoint, messages, params, timeout_s):
            seen_timeouts.append(timeout_s)
            return _resp()

        def describe_endpoint(self, endpoint):
            return {"ready": True}

    settings = _settings(model_sonnet=None)
    probe.main(["--out", str(tmp_path), "--timeout-s", "7"], client=_TimeoutSpyClient(), settings=settings)
    assert seen_timeouts and all(t == 7.0 for t in seen_timeouts)
