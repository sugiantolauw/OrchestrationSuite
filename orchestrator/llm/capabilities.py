"""Per-role parameter capability filtering (independent review 2026-09-24
item 3; docs/specs/P6_P8_explorer_llm_design.md §3.3). A role's recorded
capability matrix says which chat-completion parameters have actually been
observed to work against the currently-served model -- `capabilities.yaml`
is the recorded evidence, this module is the (small, boring) code that
enforces "never send an untested parameter" from it.

Keyed by ROLE (the Settings attribute name -- "model_sonnet"/"model_gpt_oss"),
never by literal endpoint name (CLAUDE.md §3 non-negotiable 16): the
capability matrix describes a MODEL FAMILY's behaviour, and which endpoint
currently serves that role is a config concern, not a capability concern.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_DEFAULT_PATH = Path(__file__).resolve().parent / "capabilities.yaml"


def load_capabilities(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path) if path else _DEFAULT_PATH
    return yaml.safe_load(p.read_text()) or {}


def role_config(capabilities: dict, role: str) -> dict:
    """The recorded config for one role, or an all-untested stand-in
    (`params: {}`) for a role this matrix says nothing about -- never a
    KeyError, and never a guess at what that role supports."""
    return (capabilities.get("roles") or {}).get(role) or {
        "verified_on": None, "served_model_prefix": None, "params": {},
    }


def filter_params(capabilities: dict, role: str, desired: dict) -> tuple[dict, dict[str, str]]:
    """Splits `desired` (the task's wanted chat-completion parameters) into
    `(sent, dropped)`. `sent` carries only params whose recorded capability
    is exactly "supported"; `dropped` names every withheld param with a
    reason: the recorded capability value (e.g. "rejected",
    "requires_json_word") when the matrix says something about it, or
    "untested" when the matrix is simply silent on it. Never raises --
    dropping a parameter is the whole point, not an error."""
    cfg = role_config(capabilities, role)
    params_cfg = cfg.get("params") or {}
    sent: dict = {}
    dropped: dict[str, str] = {}
    for name, value in desired.items():
        capability = params_cfg.get(name)
        if capability == "supported":
            sent[name] = value
        else:
            dropped[name] = capability if capability else "untested"
    return sent, dropped


def schema_keyword_allowed(capabilities: dict, role: str, keyword: str) -> bool:
    cfg = role_config(capabilities, role)
    return keyword not in (cfg.get("schema_keywords_rejected") or [])


def schema_property_count(schema: dict) -> int:
    """The GPT-OSS strict-JSON-schema endpoint's own property count
    (2026-09-25 live: `plan_repair` on `PLAN_PROPOSAL_SCHEMA` -- 207
    properties by this count -- got `400 BAD_REQUEST: Invalid JSON schema -
    schema has too many properties maximum allowed is 128`, while
    `plan_explorer`'s identical schema succeeded because that role's
    capability matrix carries no `json_schema_strict: supported` and so
    never reaches strict mode at all). Sums every `properties` object's key
    count found anywhere in the schema document -- the top level, `$defs`,
    and each `anyOf`/`oneOf`/array-`items` branch -- once per occurrence,
    never deduplicated by `$ref`: a schema reused by several branches (or
    inside `$defs`) costs the endpoint's grammar compiler once per place it
    is compiled in, which is what this mirrors."""
    total = 0

    def _walk(node: Any) -> None:
        nonlocal total
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                total += len(properties)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(schema)
    return total


def find_rejected_schema_keywords(capabilities: dict, role: str, schema: dict) -> list[str]:
    """Every keyword this role's matrix records as rejected (e.g.
    `pattern`) that actually appears as a KEY somewhere in `schema` --
    never a false positive from a keyword name that merely occurs as a
    property *value* (a prose string, an enum member) rather than a schema
    keyword itself."""
    rejected = set(role_config(capabilities, role).get("schema_keywords_rejected") or [])
    if not rejected:
        return []
    found: set[str] = set()

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in rejected:
                    found.add(key)
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(schema)
    return sorted(found)


def schema_strict_block_reason(capabilities: dict, role: str, schema: dict) -> str | None:
    """None when `schema` may be sent to this role as a strict
    `response_format` json_schema; otherwise the reason it may not --
    `orchestrator.llm.gateway.LLMGateway` checks this before it commits to
    the strict-mode path for a role whose `json_schema_strict` capability
    is otherwise "supported" (CLAUDE.md §6: "If structured output does not
    pass through, validate the schema client-side with one retry"). A role
    with no recorded `max_schema_properties` is never blocked on property
    count -- CLAUDE.md §6's "never assumed": this endpoint's own 128-
    property ceiling is GPT-OSS-specific, recorded evidence, not a general
    assumption applied to every future role."""
    cfg = role_config(capabilities, role)
    limit = cfg.get("max_schema_properties")
    if isinstance(limit, int):
        count = schema_property_count(schema)
        if count > limit:
            return f"schema has {count} properties, over this role's recorded limit of {limit}"
    rejected = find_rejected_schema_keywords(capabilities, role, schema)
    if rejected:
        return f"schema uses rejected keyword(s): {', '.join(rejected)}"
    return None


def served_model_matches(capabilities: dict, role: str, served_model_version: str | None) -> bool:
    """True when `served_model_version` starts with the role's recorded
    `served_model_prefix`, OR the role has never been verified against a
    live endpoint (there is nothing to mismatch yet -- the gateway's own
    "treat as all-untested" behaviour already covers that case by construction,
    since an unverified role's `params` is empty). False signals a served
    model that has moved since the matrix was recorded -- the gateway
    treats the role as all-untested and logs a health warning (docs/specs/
    P6_P8_explorer_llm_design.md §3.3)."""
    cfg = role_config(capabilities, role)
    prefix = cfg.get("served_model_prefix")
    if not prefix or not served_model_version:
        return True
    return served_model_version.startswith(prefix)
