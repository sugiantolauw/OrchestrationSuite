"""Narration prompt loader (P6 WP N7, docs/specs/P6_narration_design.md §4.3):
one shared `system_common.md` plus a per-task `<x>_user.md` template under
`orchestrator/prompts/narration/`, rendered with `string.Template.substitute`
-- never `str.format`, for the same reason
`orchestrator.llm.prompts.FilePromptRepository` gives: the rendered payload is
JSON (full of braces) and `format` permits attribute/index access a prompt
template has no business doing.

A small repository of its own, not an extension of `FilePromptRepository`,
because that class's file-resolution contract is ONE `<template_id>_system.md`
per template id (Explorer's `explorer/planner` and `explorer/repair` each own
a system file) -- the narration set is a different shape, ONE system file
shared by every task (§4.3's own wording: "one shared system_common.md plus
per-task user templates, and repair_user.md"). Bending `FilePromptRepository`
to also support a shared system file would complicate the class every other
caller of it still uses the original way, for no shared code -- the two
classes only share the `string.Template` substitution rule and the
`hash_skill_content_entries` versioning scheme, both of which this module
uses directly rather than inheriting.
"""

from __future__ import annotations

from pathlib import Path
from string import Template

from orchestrator.fingerprint import hash_skill_content_entries

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_NARRATION_PROMPTS_ROOT = _REPO_ROOT / "orchestrator" / "prompts" / "narration"

SYSTEM_FILE = "system_common.md"
REPAIR_FILE = "repair_user.md"

# round-5 narration-content review, item 1: the marker every `<task>_user.md`
# ends with (§4.3, and `test_narration_user_templates_end_with_the_payload_
# section` pins it) -- `task_rules_text` cuts there so `$task_rules` in
# repair_user.md carries the task's own instructions (its IDENTIFIERS
# section included) without a second copy of `$payload_json` itself, which
# `render_repair` fills separately via `previous_output`/`violations_json`.
_PAYLOAD_MARKER = "PAYLOAD:\n$payload_json"

# task -> its own user template file (§4.3's table). `find_candidates`
# (P6 WP N8, §5.1) is included: `orchestrator.narration.candidates` is the
# only caller (through `orchestrator.narration.runner._generate_item`, the
# same shared loop every task uses), so this map does not decide whether a
# candidate call is ever made -- the `ai_proposed_findings_enabled` switch
# does, in `orchestrator.nodes.narration.narrate`.
TASK_USER_FILES: dict[str, str] = {
    "profile": "profile_user.md",
    "find": "finding_user.md",
    "find_synthesis": "synthesis_user.md",
    "find_candidates": "candidates_user.md",
    "prioritise": "priority_user.md",
    "act": "remediation_user.md",
    "export_summary": "exec_summary_user.md",
    "export_caption": "captions_user.md",
}

__all__ = [
    "DEFAULT_NARRATION_PROMPTS_ROOT",
    "NarrationPromptError",
    "NarrationPromptRepository",
    "TASK_USER_FILES",
]


class NarrationPromptError(Exception):
    """A narration prompt file is missing, or a render() call was missing a
    placeholder its template requires -- loud, never a prompt silently
    shipped with a literal `$name` left in it (CLAUDE.md NN14), the same
    discipline `orchestrator.llm.prompts.FilePromptRepository` follows."""


class NarrationPromptRepository:
    def __init__(self, root: str | Path = DEFAULT_NARRATION_PROMPTS_ROOT):
        self.root = Path(root)

    # ── file resolution ──────────────────────────────────────────────────

    def _read(self, filename: str) -> str:
        path = self.root / filename
        if not path.is_file():
            raise NarrationPromptError(f"no narration prompt file: {path} does not exist")
        return path.read_text(encoding="utf-8")

    def _user_file(self, task: str) -> str:
        try:
            return TASK_USER_FILES[task]
        except KeyError:
            raise NarrationPromptError(f"no narration user template for task {task!r}") from None

    def system_text(self) -> str:
        return self._read(SYSTEM_FILE)

    def user_text(self, task: str) -> str:
        return self._read(self._user_file(task))

    def repair_text(self) -> str:
        return self._read(REPAIR_FILE)

    def task_rules_text(self, task: str) -> str:
        """Round-5 narration-content review, item 1: this task's own
        `<task>_user.md` instructions -- everything up to its trailing
        `PAYLOAD:\\n$payload_json` section -- taken verbatim, including its
        IDENTIFIERS section where it has one. `$generation_line` (a per-call
        regeneration hint, never a rule) is stripped out; it is never
        substituted here, since `task_rules_text` returns raw, un-rendered
        template text.

        This is what a repair call was missing (RUN-05B9661A8D1A,
        2026-09-25): `find_synthesis`'s repair round rewrote correct prose
        test ids ("T4.2") to the schema's bare finding-key form ("T4_2"),
        because `repair_user.md` carried none of `synthesis_user.md`'s own
        IDENTIFIERS rule distinguishing the two -- `render_repair` below
        fills `$task_rules` in `repair_user.md` with exactly this text, so
        the SAME rules that governed the call being repaired still apply to
        the repair itself. Mirrors `orchestrator.llm.prompts.
        FilePromptRepository.planner_rules_text`."""
        text = self.user_text(task)
        idx = text.rfind(_PAYLOAD_MARKER)
        if idx == -1:
            raise NarrationPromptError(
                f"{self._user_file(task)} has no {_PAYLOAD_MARKER!r} section to extract task rules from"
            )
        return text[:idx].replace("$generation_line", "").strip()

    # ── versioning (§1 #8, §6.3): one hash over the whole set, feeding the
    # run fingerprint's prompt_template_version the same way a Skill's own
    # content hash and FilePromptRepository.template_set_version do ──────

    def template_set_version(self) -> str:
        filenames = sorted({SYSTEM_FILE, REPAIR_FILE, *TASK_USER_FILES.values()})
        entries = [(name, (self.root / name).read_bytes()) for name in filenames]
        return hash_skill_content_entries(entries)

    # ── rendering ────────────────────────────────────────────────────────

    def render_task(self, task: str, **params) -> list[dict]:
        """`system_common.md` + this task's own `<x>_user.md`, both
        rendered with the SAME `params` -- `payload_json` and
        `generation_line` are supplied by every task's own user template
        (§4.3), plus whatever extra placeholders that one task's template
        additionally needs (e.g. `find_candidates`'s `$max_candidates`,
        N8's concern)."""
        system_text = self.system_text()
        user_text = self._substitute(f"narration/{task}", "user", self.user_text(task), params)
        return [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ]

    def render_repair(
        self, task: str, *, violations_json: str, previous_output: str, **task_params,
    ) -> list[dict]:
        """§3.5's repair call: the SAME system prompt (so every rule in it
        still applies), the SAME task/schema, but `repair_user.md` in place
        of the task's own user template -- listing the task's own rules
        (`task_rules_text`, round-5 item 1), what broke and the prior
        output, asking for a corrected JSON object rather than a fresh
        generation.

        `**task_params` is whatever extra placeholder(s) this task's own
        user template needs beyond `payload_json`/`generation_line` (e.g.
        `find_candidates`'s `$max_candidates`, still present verbatim inside
        the extracted task-rules text) -- the same `extra_params` the
        caller already passes to `render_task` for the generate round, so
        the repair round's copy of those rules renders identically rather
        than being left with a stray, un-substituted `$name`."""
        system_text = self.system_text()
        task_rules = self._substitute(
            f"narration/{task}/task_rules", "task_rules", self.task_rules_text(task), task_params,
        )
        user_text = self._substitute(
            f"narration/{task}/repair", "user", self.repair_text(),
            {"violations_json": violations_json, "previous_output": previous_output, "task_rules": task_rules},
        )
        return [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ]

    @staticmethod
    def _substitute(template_id: str, part: str, text: str, params: dict) -> str:
        try:
            return Template(text).substitute(**params)
        except KeyError as exc:
            raise NarrationPromptError(
                f"{template_id} ({part}): missing placeholder {exc} -- every $name in a narration "
                f"prompt template must be supplied by the caller (CLAUDE.md NN14)"
            ) from exc
