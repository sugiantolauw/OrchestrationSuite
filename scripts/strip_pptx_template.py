"""One-off generator for templates/report_template.pptx (CLAUDE.md §4.7):
a content-free copy of a source PPTX deck -- every slide MASTER and LAYOUT
kept (so the PPTX exporter can still pick a layout by name/index), every
actual content SLIDE removed, so the exporter never inherits stray
placeholder text, a stray chart or a stray "1"/"2"/... page number from the
deck it was built from. `--source` is required and never hardcoded here
(CLAUDE.md §3 non-negotiable 16 -- this script names no organisation's own
template file; portability is an environment/command-line change, not a
code change).

This script is the one place allowed to reach into python-pptx internals
(`prs.slides._sldIdLst`, `part.drop_rel`) to do that removal -- the runtime
exporter (orchestrator/pptx_export.py) must never do the same (CLAUDE.md
§4.7 defect 6: reference_app/src/pptx_export.py's generate_pptx() did this
on every single export call, which is exactly the private-API fragility
this build brief calls out to fix). Run once, by a human, to (re)generate
the committed template; never imported by the pipeline.

Usage:
    python scripts/strip_pptx_template.py \\
        --source path/to/the/corporate/deck/template.pptx \\
        --out templates/report_template.pptx
"""

from __future__ import annotations

import argparse
from pathlib import Path

from pptx import Presentation
from pptx.opc.constants import RELATIONSHIP_TYPE as RT

REPO_ROOT = Path(__file__).resolve().parent.parent
_NS_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def strip_content_slides(source: Path, out: Path) -> None:
    prs = Presentation(str(source))
    slide_ids = list(prs.slides._sldIdLst)
    for sld_id in slide_ids:
        r_id = sld_id.get(_NS_ID)
        if r_id is not None:
            try:
                prs.part.drop_rel(r_id)
            except KeyError:
                pass
        prs.slides._sldIdLst.remove(sld_id)

    # The source deck's own presentation.xml.rels can carry slide
    # relationships that were never in <p:sldIdLst> to begin with -- this
    # project's own source template ships 2 such dangling rels, both still
    # pointing at the same already-orphaned slide part, left over from
    # whatever authoring history produced that template. OpcPackage.save()
    # writes every part still REACHABLE via some relationship
    # (opc/package.py's iter_parts(), a graph walk, not "every part
    # physically in the source zip"), so an orphaned-from-sldIdLst but
    # still-related slide part would otherwise survive stripping and ship
    # inside the "content-free" template -- exactly the duplicate-partname
    # collision this loop exists to prevent the next time add_slide() picks
    # a name.
    dangling = [rId for rId, rel in prs.part.rels.items() if rel.reltype == RT.SLIDE]
    for r_id in dangling:
        prs.part.drop_rel(r_id)

    assert len(prs.slides._sldIdLst) == 0, "content slides remain after stripping"
    assert not any(rel.reltype == RT.SLIDE for rel in prs.part.rels.values()), (
        "a dangling (not-in-sldIdLst) slide relationship survived stripping"
    )
    assert not any(str(p.partname).startswith("/ppt/slides/") for p in prs.part.package.iter_parts()), (
        "a slide part is still reachable in the package after stripping"
    )
    assert len(prs.slide_layouts) > 0, "layouts were lost -- re-check the source template"
    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="the source corporate deck template (.pptx)")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "templates" / "report_template.pptx")
    args = parser.parse_args()
    strip_content_slides(args.source, args.out)
    print(f"wrote {args.out} ({args.out.stat().st_size:,} bytes, 0 slides, "
          f"{len(Presentation(str(args.out)).slide_layouts)} layouts)")


if __name__ == "__main__":
    main()
