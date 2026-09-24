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

After stripping slides, `_scrub_package_metadata` sanitises the package's
OWN metadata (never the visual design a corporate brand team owns -- no
layout, colour, font or logo image is touched): a source deck saved from
Microsoft 365 co-authoring carries collaboration-history parts
(`ppt/changesInfos/*.xml`, `ppt/revisionInfo.xml`) that embed named authors'
email addresses, a sensitivity-label part (`docMetadata/LabelInfo.xml`) tied
to the source org's tenant, a preview thumbnail rendered from the source
deck's own first slide (which can show real people, titles and project
names), and `docProps/core.xml` / `app.xml` fields (creator, last-modified-
by, manager, description, and a stale `TitlesOfParts` list of the very
slide titles this script just removed) an independent NN16 review found
carrying a real name, phone number and internal project codename in this
project's own source deck. None of that is identified by literal string
match against this one source deck -- every part/field is found generically
(by OOXML relationship type for the parts, by field name for the docProps
values), so this also sanitises whatever a *different* corporate source
deck carries the next time this script runs against one.
"""

from __future__ import annotations

import argparse
import posixpath
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from pptx import Presentation
from pptx.opc.constants import RELATIONSHIP_TYPE as RT

REPO_ROOT = Path(__file__).resolve().parent.parent
_NS_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"

_NS_EP = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
_NS_VT = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
_NS_CP = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
_NS_DC = "http://purl.org/dc/elements/1.1/"
_NS_DCTERMS = "http://purl.org/dc/terms/"
_NS_DCMITYPE = "http://purl.org/dc/dcmitype/"
_NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"

# Relationship TYPES (never part names -- a different source deck's part
# names/rIds will differ) that identify collaboration-history, sensitivity-
# label and preview-thumbnail parts: metadata this project's own template
# has no use for and that independently identifies the source org/authors.
_DROP_REL_TYPE_SUBSTRINGS = (
    "relationships/changesInfo",
    "relationships/revisionInfo",
    "relationships/classificationlabels",
    "relationships/metadata/thumbnail",
)

_REL_RE = re.compile(rb"<Relationship\b[^>]*/>")
_REL_ID_RE = re.compile(rb'\bId="([^"]*)"')
_REL_TYPE_RE = re.compile(rb'\bType="([^"]*)"')
_REL_TARGET_RE = re.compile(rb'\bTarget="([^"]*)"')
_OVERRIDE_RE = re.compile(rb"<Override\b[^>]*/>")
_OVERRIDE_PARTNAME_RE = re.compile(rb'\bPartName="([^"]*)"')
_DEFAULT_RE = re.compile(rb"<Default\b[^>]*/>")
_DEFAULT_EXT_RE = re.compile(rb'\bExtension="([^"]*)"')


def _rels_base_dir(rels_part: str) -> str:
    """'ppt/_rels/presentation.xml.rels' -> 'ppt'; '_rels/.rels' -> ''."""
    without_suffix = rels_part[: -len(".rels")]
    parts = without_suffix.split("/")
    assert parts[-2] == "_rels", rels_part
    return "/".join(parts[:-2])


def _resolve_target(rels_part: str, target: bytes) -> str:
    target_str = target.decode("utf-8")
    if target_str.startswith("/"):
        return target_str.lstrip("/")
    base = _rels_base_dir(rels_part)
    return posixpath.normpath(posixpath.join(base, target_str) if base else target_str)


def _sanitize_core_xml(data: bytes) -> bytes:
    """Blanks author-identifying fields by NAME, not by the value this one
    source deck happened to carry -- 'Sugianto Lauw' / 'Contact: 1300 843
    562' were this project's source deck's own dc:creator/dc:description;
    a different deck's real values are caught the same way."""
    ET.register_namespace("cp", _NS_CP)
    ET.register_namespace("dc", _NS_DC)
    ET.register_namespace("dcterms", _NS_DCTERMS)
    ET.register_namespace("dcmitype", _NS_DCMITYPE)
    ET.register_namespace("xsi", _NS_XSI)
    root = ET.fromstring(data)
    for tag, ns in (("creator", _NS_DC), ("description", _NS_DC), ("lastModifiedBy", _NS_CP)):
        el = root.find(f"{{{ns}}}{tag}")
        if el is not None:
            el.text = None
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


def _sanitize_app_xml(data: bytes) -> bytes:
    """Blanks the Manager field (a named author, same reasoning as
    _sanitize_core_xml) and rebuilds HeadingPairs/TitlesOfParts to drop the
    'Slide Titles' category entirely -- those are the titles of the exact
    content slides `strip_content_slides` just removed, so the moment this
    package has 0 slides, listing 12 stale titles (an internal project
    codename among them, in this project's own source deck) is not just a
    leak, it is also simply wrong. The 'Theme' category (the template's own
    theme names) is left untouched -- that is the visual design this script
    must not alter, merely accurately reported."""
    ET.register_namespace("", _NS_EP)
    ET.register_namespace("vt", _NS_VT)
    root = ET.fromstring(data)

    def find(tag):
        return root.find(f"{{{_NS_EP}}}{tag}")

    for tag in ("Manager",):
        el = find(tag)
        if el is not None:
            el.text = None
    for tag, value in (("Slides", "0"), ("Notes", "0"), ("HiddenSlides", "0")):
        el = find(tag)
        if el is not None:
            el.text = value

    heading_pairs = find("HeadingPairs")
    titles_of_parts = find("TitlesOfParts")
    if heading_pairs is not None and titles_of_parts is not None:
        hp_vector = heading_pairs.find(f"{{{_NS_VT}}}vector")
        variants = hp_vector.findall(f"{{{_NS_VT}}}variant") if hp_vector is not None else []
        pairs = []
        for i in range(0, len(variants) - 1, 2):
            name_el = variants[i].find(f"{{{_NS_VT}}}lpstr")
            count_el = variants[i + 1].find(f"{{{_NS_VT}}}i4")
            name = name_el.text if name_el is not None else None
            count = int(count_el.text) if count_el is not None and count_el.text else 0
            pairs.append((name, count, count_el))

        top_vector = titles_of_parts.find(f"{{{_NS_VT}}}vector")
        top_lpstrs = top_vector.findall(f"{{{_NS_VT}}}lpstr") if top_vector is not None else []
        idx = 0
        keep = []
        for name, count, count_el in pairs:
            chunk = top_lpstrs[idx: idx + count]
            idx += count
            if name == "Slide Titles":
                if count_el is not None:
                    count_el.text = "0"
            else:
                keep.extend(chunk)
        if top_vector is not None:
            for el in list(top_vector):
                top_vector.remove(el)
            for el in keep:
                top_vector.append(el)
            top_vector.set("size", str(len(keep)))
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


def _scrub_package_metadata(path: Path) -> None:
    with zipfile.ZipFile(path, "r") as zin:
        infos = zin.infolist()
        contents = {info.filename: zin.read(info.filename) for info in infos}

    rels_files = [name for name in contents if name.endswith(".rels")]
    drop_parts: set[str] = set()
    for rels_name in rels_files:
        data = contents[rels_name]
        for rel in _REL_RE.findall(data):
            type_m = _REL_TYPE_RE.search(rel)
            target_m = _REL_TARGET_RE.search(rel)
            if not type_m or not target_m:
                continue
            rel_type = type_m.group(1).decode("utf-8")
            if any(sub in rel_type for sub in _DROP_REL_TYPE_SUBSTRINGS):
                drop_parts.add(_resolve_target(rels_name, target_m.group(1)))

    if not drop_parts:
        return

    for rels_name in rels_files:
        data = contents[rels_name]

        def _strip_rel(m: "re.Match[bytes]") -> bytes:
            rel = m.group(0)
            target_m = _REL_TARGET_RE.search(rel)
            if target_m and _resolve_target(rels_name, target_m.group(1)) in drop_parts:
                return b""
            return rel

        contents[rels_name] = _REL_RE.sub(_strip_rel, data)

    if "[Content_Types].xml" in contents:
        data = contents["[Content_Types].xml"]

        def _strip_override(m: "re.Match[bytes]") -> bytes:
            override = m.group(0)
            name_m = _OVERRIDE_PARTNAME_RE.search(override)
            if name_m and name_m.group(1).decode("utf-8").lstrip("/") in drop_parts:
                return b""
            return override

        data = _OVERRIDE_RE.sub(_strip_override, data)
        # Extensions of the parts that will actually SURVIVE this pass --
        # not "every extension minus the dropped parts' own", which wrongly
        # drops "xml" itself the moment any dropped part happens to be
        # *.xml, even though hundreds of other *.xml parts remain.
        remaining_exts = {
            name.rsplit(".", 1)[-1].lower()
            for name in contents
            if "." in name and name not in drop_parts
        }

        def _strip_unused_default(m: "re.Match[bytes]") -> bytes:
            default = m.group(0)
            ext_m = _DEFAULT_EXT_RE.search(default)
            if ext_m and ext_m.group(1).decode("utf-8").lower() not in remaining_exts:
                return b""
            return default

        data = _DEFAULT_RE.sub(_strip_unused_default, data)
        contents["[Content_Types].xml"] = data

    if "docProps/core.xml" in contents:
        contents["docProps/core.xml"] = _sanitize_core_xml(contents["docProps/core.xml"])
    if "docProps/app.xml" in contents:
        contents["docProps/app.xml"] = _sanitize_app_xml(contents["docProps/app.xml"])

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in infos:
            if info.filename in drop_parts:
                continue
            zout.writestr(info.filename, contents[info.filename])


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

    _scrub_package_metadata(out)
    with zipfile.ZipFile(out, "r") as zcheck:
        names = set(zcheck.namelist())
        assert "docProps/thumbnail.jpeg" not in names, "source-deck preview thumbnail survived scrubbing"
        for name in ("docProps/core.xml", "docProps/app.xml"):
            text = zcheck.read(name).decode("utf-8", errors="replace")
            assert not re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text), (
                f"an email address survived scrubbing {name}"
            )
        for name in names:
            if name.endswith(".rels") or name == "[Content_Types].xml":
                text = zcheck.read(name).decode("utf-8", errors="replace")
                for sub in _DROP_REL_TYPE_SUBSTRINGS:
                    assert sub not in text, f"a dropped-part relationship/content-type survived in {name}"


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
