"""TASK_PROFILES: the desired chat-completion parameters per LLM task
(docs/specs/P6_P8_explorer_llm_design.md §3.4). A node builds its
`desired_params` from here rather than inventing them inline, so every task's
parameters are declared once, in one place, and `orchestrator.llm.capabilities.
filter_params` is what actually decides which of them are sent for the task's
resolved role (orchestrator.config.NODE_MODELS) -- this module never talks to
capabilities.yaml itself.

Only the two Explorer plan-node tasks are populated in this step; the
narration tasks (`find`, `profile`, `prioritise`, `act`, `export_summary`,
`export_caption`, `classify`, the judges) get profiles when their narration
is built (§10 "Deferred")."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TaskProfile:
    role: str  # orchestrator.config.NODE_MODELS[task] must agree with this (see test)
    desired_params: dict = field(default_factory=dict)
    structured_output: bool = False
    fallback: str | None = None  # None = no fallback; degrade per §3.8/NN13


TASK_PROFILES: dict[str, TaskProfile] = {
    "plan_explorer": TaskProfile(
        role="model_sonnet",
        desired_params={"max_tokens": 16000, "temperature": 0},
        structured_output=True,
        fallback=None,
    ),
    "plan_repair": TaskProfile(
        role="model_gpt_oss",
        desired_params={"max_tokens": 16000, "temperature": 0, "reasoning_effort": "medium"},
        structured_output=True,
        fallback=None,
    ),
}
