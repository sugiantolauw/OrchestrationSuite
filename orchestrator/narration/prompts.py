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

# task -> its own user template file (§4.3's table). `find_candidates` is
# N8's task (WP N7 leaves a clean hook, per the coordinator's instructions
# for this WP) -- its template file already exists on disk (candidates_user.md,
# merged ahead of this WP) but is deliberately left out of this map so N7's
# runner can never accidentally call it.
TASK_USER_FILES: dict[str, str] = {
    "profile": "profile_user.md",
    "find": "finding_user.md",
    "find_synthesis": "synthesis_user.md",
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

    def render_repair(self, task: str, *, violations_json: str, previous_output: str) -> list[dict]:
        """§3.5's repair call: the SAME system prompt (so every rule in it
        still applies), the SAME task/schema, but `repair_user.md` in place
        of the task's own user template -- listing what broke and the
        prior output, asking for a corrected JSON object rather than a
        fresh generation."""
        system_text = self.system_text()
        user_text = self._substitute(
            f"narration/{task}/repair", "user", self.repair_text(),
            {"violations_json": violations_json, "previous_output": previous_output},
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
