"""Platform Skill Library page — wires the "Search skills…" input and the
Domain / Status dropdowns.

BUG-SKILLS-1 (this task): `skill_library_page()` (src/platform/pages.py)
renders the `skill-search` dcc.Input and the `skill-domain-filter` /
`skill-status-filter` dcc.Dropdowns exactly as the prototype does (same
ids, same options, same text — CLAUDE.md §11 "UI is the prototype's,
exactly"), but neither the prototype nor this build ever wired a callback
to them, so typing a search term or picking a domain/status never filtered
the skill card list. This module does only that, mirroring src/trace_page.py's
and src/runs_page.py's own register_callbacks pattern (registered alongside
them from app/app.py).

Reads go through adapters.list_skills() — the same single, unfiltered call
skill_library_page() itself already makes to build the page and its filter
dropdown options — filtering happens in Python over that one fetch, never a
second query per filter value.

Search matches the skill's name, id or description (case-insensitive
substring match on any of the three); a cleared search box (None or "")
applies no filter. The Domain and Status dropdowns match case-insensitively
against the skill's own `domain`/`status` field; a cleared dropdown (None
or "") applies no filter. All three filters combine with AND, exactly the
runs page's own `runs-skill-filter` / `runs-status-filter` combination
(src/runs_page.py)."""

from __future__ import annotations

from dash import Input, Output

from src.platform import adapters
from src.platform.components import skill_card


def _matches_filter(actual, wanted: str | None) -> bool:
    """See src/runs_page.py's own `_matches`: Dash's dcc.Dropdown clears to
    `None` or `""` depending on how the clear was triggered — either means
    "no filter" — and the comparison is case-insensitive."""
    if not wanted:
        return True
    return str(actual or "").casefold() == wanted.casefold()


def _matches_search(skill: dict, query: str | None) -> bool:
    if not query:
        return True
    needle = query.casefold()
    haystack = " ".join(
        str(skill.get(field) or "") for field in ("name", "skill_id", "description")
    ).casefold()
    return needle in haystack


def _rows_for_filter(search: str | None, domain: str | None, status: str | None) -> list:
    skills = adapters.list_skills()
    filtered = [
        s for s in skills
        if _matches_search(s, search)
        and _matches_filter(s.get("domain"), domain)
        and _matches_filter(s.get("status"), status)
    ]
    return [skill_card(s) for s in filtered]


def register_callbacks(app) -> None:

    @app.callback(
        Output("skill-library-grid", "children"),
        Input("skill-search", "value"),
        Input("skill-domain-filter", "value"),
        Input("skill-status-filter", "value"),
    )
    def _filter_skills(search, domain, status):
        return _rows_for_filter(search, domain, status)
