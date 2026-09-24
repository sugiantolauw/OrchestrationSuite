"""TASK_PROFILES: the desired chat-completion parameters per LLM task
(docs/specs/P6_P8_explorer_llm_design.md §3.4). A node builds its
`desired_params` from here rather than inventing them inline, so every task's
parameters are declared once, in one place, and `orchestrator.llm.capabilities.
filter_params` is what actually decides which of them are sent for the task's
resolved role (orchestrator.config.NODE_MODELS) -- this module never talks to
capabilities.yaml itself.

The two Explorer plan-node tasks, plus the eight narration tasks (P6 WP N5,
docs/specs/P6_narration_design.md §4.1's routing/parameter table) are
populated here. `classify` and the judges are not: `classify` builds its own
`ai_query()`-shaped params inline (orchestrator.nodes.fieldwork), and the
judge tasks are a later work package (§10 "Deferred")."""

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

    # ── narration tasks (P6 WP N5, docs/specs/P6_narration_design.md §4.1's
    # own table). Every one returns a structured JSON object (one of the
    # orchestrator.narration.schemas builders); `fallback` mirrors
    # FALLBACK_ROLE below exactly -- profile/prioritise/act are
    # auditor-edited prose in a consistent voice and may degrade to
    # GPT-OSS (CLAUDE.md §6), everything else has none (never silently
    # degrade what a CAO/executive reads verbatim, NN13). ─────────────────
    "profile": TaskProfile(
        role="model_sonnet",
        desired_params={"max_tokens": 800, "temperature": 0},
        structured_output=True,
        fallback="model_gpt_oss",
    ),
    "find": TaskProfile(
        role="model_sonnet",
        desired_params={"max_tokens": 1500, "temperature": 0},
        structured_output=True,
        fallback=None,
    ),
    "find_synthesis": TaskProfile(
        role="model_sonnet",
        desired_params={"max_tokens": 3000, "temperature": 0},
        structured_output=True,
        fallback=None,
    ),
    "find_candidates": TaskProfile(
        role="model_sonnet",
        desired_params={"max_tokens": 4000, "temperature": 0},
        structured_output=True,
        fallback=None,
    ),
    "prioritise": TaskProfile(
        role="model_sonnet",
        desired_params={"max_tokens": 2000, "temperature": 0},
        structured_output=True,
        fallback="model_gpt_oss",
    ),
    "act": TaskProfile(
        role="model_sonnet",
        desired_params={"max_tokens": 3000, "temperature": 0},
        structured_output=True,
        fallback="model_gpt_oss",
    ),
    "export_summary": TaskProfile(
        role="model_sonnet",
        desired_params={"max_tokens": 1500, "temperature": 0},
        structured_output=True,
        fallback=None,
    ),
    "export_caption": TaskProfile(
        role="model_gpt_oss",
        desired_params={"max_tokens": 600, "temperature": 0, "reasoning_effort": "low"},
        structured_output=True,
        fallback=None,
    ),
}

# FALLBACK_ROLE (CLAUDE.md §6 "Fallback" / docs/specs/P6_narration_design.md
# §4.1): the per-task degrade rule LLMGateway.call() applies when a task's
# primary role (orchestrator.config.NODE_MODELS[task]) is unavailable.
# profile/prioritise/act are auditor-edited prose in a consistent voice --
# CLAUDE.md §6 allows degrading them to GPT-OSS. `find`, the exec summary
# and `plan` are read verbatim by a CAO/executive or drive what an auditor
# confirms, and CLAUDE.md §6 says never to silently degrade what an
# executive reads -- they, and every task absent from this dict, have no
# fallback: LLMGateway.call() returns `status="unavailable"` for them
# instead ("LLM unavailable — deterministic output only", NN13). A task
# whose fallback role resolves to the SAME endpoint as its primary role
# (the development-workspace override, CLAUDE.md §6) is skipped by the
# gateway, not listed specially here -- that is a runtime config fact, not
# a per-task rule.
FALLBACK_ROLE: dict[str, str | None] = {
    "profile": "model_gpt_oss",
    "prioritise": "model_gpt_oss",
    "act": "model_gpt_oss",
}
