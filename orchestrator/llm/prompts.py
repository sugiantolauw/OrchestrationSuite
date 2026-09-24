"""FilePromptRepository (docs/specs/P6_P8_explorer_llm_design.md §3.10):
loads the Explorer prompt templates under orchestrator/prompts/explorer/ and
renders them with `string.Template.substitute` -- deliberately NOT
`str.format`, because the rendered payloads are JSON (which contains braces)
and `format` permits attribute/index access a prompt template has no
business doing. `substitute` (not `safe_substitute`) is used throughout, so a
caller that forgot to supply a placeholder gets a loud, named failure rather
than a prompt silently shipped with a literal `$name` in it (CLAUDE.md
NN14).

`template_id` is `"explorer/planner"` or `"explorer/repair"`; each maps to a
`<id>_system.md` / `<id>_user.md` pair under `root`. `version` is the sha256
over both files' repo-relative paths and bytes, reusing
`orchestrator.fingerprint.hash_skill_content_entries` -- the same hashing a
Skill's own content_hash uses, so there is one hashing implementation in the
codebase, not two that could drift (§3.10: "the same hashing as
fingerprint._hash_entries")."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from string import Template

from orchestrator.fingerprint import hash_skill_content_entries

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PROMPTS_ROOT = _REPO_ROOT / "orchestrator" / "prompts"


class PromptTemplateError(Exception):
    """A template is missing, or a render() call was missing a placeholder
    the template requires. Never silently rendered with a literal `$name`
    left in (CLAUDE.md NN14)."""


@dataclass(frozen=True)
class PromptTemplate:
    template_id: str
    system: str
    user: str
    version: str


class FilePromptRepository:
    def __init__(self, root: str | Path = DEFAULT_PROMPTS_ROOT):
        self.root = Path(root)

    # ── file resolution ──────────────────────────────────────────────────

    def _paths(self, template_id: str) -> tuple[Path, Path]:
        system_path = self.root / f"{template_id}_system.md"
        user_path = self.root / f"{template_id}_user.md"
        return system_path, user_path

    def _entries(self, template_id: str) -> list[tuple[str, bytes]]:
        system_path, user_path = self._paths(template_id)
        entries: list[tuple[str, bytes]] = []
        for path in (system_path, user_path):
            if not path.is_file():
                raise PromptTemplateError(
                    f"no prompt template file for {template_id!r}: {path} does not exist"
                )
            entries.append((str(path.relative_to(self.root)), path.read_bytes()))
        return entries

    # ── public API (§3.10) ──────────────────────────────────────────────

    def get_template(self, template_id: str) -> PromptTemplate:
        entries = self._entries(template_id)
        by_path = dict(entries)
        system_path, user_path = self._paths(template_id)
        system_text = by_path[str(system_path.relative_to(self.root))].decode("utf-8")
        user_text = by_path[str(user_path.relative_to(self.root))].decode("utf-8")
        return PromptTemplate(
            template_id=template_id,
            system=system_text,
            user=user_text,
            version=hash_skill_content_entries(entries),
        )

    def template_set_version(self, template_ids: list[str]) -> str:
        union: dict[str, bytes] = {}
        for template_id in template_ids:
            union.update(dict(self._entries(template_id)))
        return hash_skill_content_entries(sorted(union.items()))

    def planner_rules_text(self) -> str:
        """The numbered rules block from explorer/planner_system.md, taken
        verbatim -- $planner_rules in repair_system.md is filled with exactly
        this text at render time, so there is one source of truth for what
        "all planner rules still apply" means (§6.3)."""
        template = self.get_template("explorer/planner")
        marker = "Rules:\n"
        idx = template.system.find(marker)
        if idx == -1:
            raise PromptTemplateError(
                "explorer/planner_system.md has no 'Rules:' section to extract for repair"
            )
        return template.system[idx + len(marker):].strip()

    def render(self, template_id: str, **params) -> list[dict]:
        """Renders one template into chat messages
        `[{"role": "system", "content": ...}, {"role": "user", "content": ...}]`.
        For "explorer/repair", `planner_rules` defaults to
        `planner_rules_text()` when the caller did not supply it explicitly."""
        template = self.get_template(template_id)
        if template_id == "explorer/repair" and "planner_rules" not in params:
            params = {**params, "planner_rules": self.planner_rules_text()}
        system_text = self._substitute(template_id, "system", template.system, params)
        user_text = self._substitute(template_id, "user", template.user, params)
        return [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ]

    @staticmethod
    def _substitute(template_id: str, part: str, text: str, params: dict) -> str:
        try:
            return Template(text).substitute(**params)
        except KeyError as exc:
            raise PromptTemplateError(
                f"{template_id} ({part}): missing placeholder {exc} -- every $name in a prompt "
                f"template must be supplied by the caller (CLAUDE.md NN14)"
            ) from exc
