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
