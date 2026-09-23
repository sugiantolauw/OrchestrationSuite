"""Extract (tag, id, className) tuples in DOM order from a Dash component tree."""
from __future__ import annotations


def shape(node, out=None, depth=0):
    if out is None:
        out = []
    if node is None:
        return out
    if isinstance(node, (list, tuple)):
        for n in node:
            shape(n, out, depth)
        return out
    if isinstance(node, (str, int, float)):
        return out
    # Dash component
    tag = type(node).__name__
    _id = getattr(node, "id", None)
    cls = getattr(node, "className", None)
    out.append((depth, tag, _id, cls))
    children = getattr(node, "children", None)
    if children is not None:
        shape(children, out, depth + 1)
    return out


def render(lines):
    return "\n".join(f"{'  '*d}{tag} id={_id!r} class={cls!r}" for d, tag, _id, cls in lines)
