"""A reduced structural signature for pages whose ROW COUNT legitimately
differs between the prototype's fixed demo fixture and a real completed
run's data (CLAUDE.md item E's "matching-volume harness" for /workspace/tne
and /skills/<id>): section headings, tab ids, KPI labels, chart ids and
table headers must still match the prototype exactly; the number of table
rows a real run happens to produce is excluded on purpose by never
descending into a Tbody's children (headers live in the sibling Thead, so
they are still captured).

Unlike tree.py's shape()/render() (tag/id/className only, used for the
zero-diff pages), this also reads label TEXT for a handful of landmark
kinds -- headings, KPI titles, table header cells -- because two KPI tiles
or two headings can share the same tag/class and only differ in the words
they show; matching only structure would silently let "Findings" become
"Exceptions" without failing anything.
"""

from __future__ import annotations

import re

_HEADING_TAGS = {"H2", "H3", "H4"}
_DIGITS_RE = re.compile(r"\d[\d,.]*")


def _normalize(text: str) -> str:
    """Collapses any run-specific number (a finding count, a dollar figure,
    a percentage) to '#' before comparison. A completed local-backend run
    over synthetic_data and the prototype's own fixed/random demo data will
    never agree on the actual numbers a headline like "3 high-priority
    matter(s) across 5 findings" embeds -- CLAUDE.md item E excludes exactly
    that kind of data-dependent content, not just table row counts."""
    return _DIGITS_RE.sub("#", text).strip()


def _text(node) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, (int, float)):
        return str(node)
    if isinstance(node, (list, tuple)):
        return "".join(_text(c) for c in node)
    return _text(getattr(node, "children", None))


def landmarks(node, out=None) -> dict:
    if out is None:
        out = {
            "headings": [], "tab_ids": [], "kpi_labels": [], "chart_ids": [], "table_headers": [],
        }
    if node is None or isinstance(node, (str, int, float)):
        return out
    if isinstance(node, (list, tuple)):
        for n in node:
            landmarks(n, out)
        return out

    tag = type(node).__name__
    _id = getattr(node, "id", None)
    cls = getattr(node, "className", None) or ""

    if tag in _HEADING_TAGS:
        out["headings"].append(_normalize(_text(node)))
    if tag == "Tabs" and _id is not None:
        out["tab_ids"].append(["Tabs", _id])
    if tag == "Tab":
        tab_id = getattr(node, "tab_id", None)
        if tab_id is not None:
            out["tab_ids"].append(["Tab", tab_id])
    if tag == "Graph" and _id is not None:
        out["chart_ids"].append(_id)
    if tag in ("Div", "Span", "P") and "kpi-title" in cls.split():
        out["kpi_labels"].append(_normalize(_text(node)))
    if tag == "Th":
        out["table_headers"].append(_normalize(_text(node)))

    children = getattr(node, "children", None)
    # Never descend into a table body: that is exactly the row-count
    # variation between the prototype's fixed demo data and a real
    # completed run this harness must NOT flag. Headers live in the
    # sibling Thead, not inside Tbody, so they are unaffected.
    if children is not None and tag != "Tbody":
        landmarks(children, out)
    return out


def render(lm: dict) -> str:
    lines = []
    for key in ("headings", "tab_ids", "kpi_labels", "chart_ids", "table_headers"):
        lines.append(f"{key}:")
        for v in lm[key]:
            lines.append(f"  {v!r}")
    return "\n".join(lines)
