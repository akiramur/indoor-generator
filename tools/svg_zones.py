"""Vector seating chart (SVG, or a PDF page converted with pdftocairo) -> a floor's `*-trace.json` of zone outlines.

    pdftocairo -svg chart.pdf chart.svg
    pdftoppm -r 288 -png chart.pdf chart            # the image the traced pixels refer to (288 dpi = 4 px per pt)
    .venv/bin/python tools/svg_zones.py chart.svg out-trace.json --image chart.png --legend legend.json [--list] \
        [--category seating] [--source s1] [--min-area 150]

Every filled, non-white shape becomes one zone, in pixels of the converted image (`--image` gives its size, from which the scale
follows; or `--scale` px per SVG unit. Note a `pt` SVG is read in CSS px, 4/3 of its pt size), ready for `main.py trace` after `main.py basemap --image chart.png`. Curves are
flattened. Legend swatches (plain rectangles) are dropped; text glyphs by `--min-area` (SVG units²) and by `--legend`.

`legend.json` maps a fill to a name, `{"#2563b4": "指定席FC", ...}` (any `#rrggbb`; `--list` prints every fill found with
its shape count, area and a hex to copy, so the legend can be written from the chart's own key). A name can carry a
`category`: `{"#2563b4": {"name": "指定席FC", "category": "seating", "tier": 1}}`; other keys are copied onto the zone.
Zones of one fill get `-1`, `-2` ... suffixes when there are several. Fills not in the legend are skipped (`--keep-unnamed`
keeps them as `zone-<hex>`).
"""

from __future__ import annotations

import argparse
import io
import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
import pathlib

import numpy as np
from shapely.geometry import Polygon
from svgelements import SVG, Path, Shape

STEPS_PER_CURVE = 12


def _hex(color) -> str | None:
    if color is None or color.value is None:
        return None
    r, g, b = color.red, color.green, color.blue
    return "#%02x%02x%02x" % (int(r), int(g), int(b))


def _rings(shape) -> list[list[tuple[float, float]]]:
    """A shape's closed sub-paths as point lists, curves flattened, transforms applied."""
    path = Path(shape) * shape.transform if not isinstance(shape, Path) else shape
    path = abs(path)
    rings: list[list[tuple[float, float]]] = []
    for sub in path.as_subpaths():
        pts = _flatten(Path(sub))
        if len(pts) >= 3:
            rings.append(pts)
    return rings


def _flatten(path: Path) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for seg in path:
        name = type(seg).__name__
        if name == "Move":
            pts.append((seg.end.x, seg.end.y))
        elif name in ("Line", "Close"):
            if seg.end is not None:
                pts.append((seg.end.x, seg.end.y))
        else:
            for i in range(1, STEPS_PER_CURVE + 1):
                p = seg.point(i / STEPS_PER_CURVE)
                pts.append((p.x, p.y))
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    return pts


SVG_NS = "http://www.w3.org/2000/svg"


def unwrap_blend_groups(text: str) -> str:
    """pdftocairo draws a chart with multiply / transparency as `compositing-group-N` groups inside `<defs>`, shown
    through a mask + filter and an opposite `translate` on the page: nothing a plain SVG reader draws. Hoist the groups
    that hold shapes out of `<defs>` (under that page translate, which cancels the groups' own), drop the masks, filters and the groups that only blend."""
    root = ET.fromstring(text)
    q = lambda tag: f"{{{SVG_NS}}}{tag}"
    defs = root.find(q("defs"))
    shifted = next((g for g in root.findall(q("g")) if g.get("filter") and "translate" in (g.get("transform") or "")), None)
    if defs is None or shifted is None:
        return text
    hoisted = ET.Element(q("g"), {"transform": shifted.get("transform")})
    for g in list(defs):
        if g.tag == q("g") and (g.get("id") or "").startswith("compositing-group") and g.find(f".//{q('path')}") is not None:
            defs.remove(g)
            hoisted.append(g)
    for el in root.iter():
        for attr in ("mask", "filter"):
            el.attrib.pop(attr, None)
    root.remove(shifted)
    root.append(hoisted)
    return ET.tostring(root, encoding="unicode")


def _is_swatch(poly: Polygon) -> bool:
    """A plain rectangle (legend key box, background): zones of a seating chart are curved."""
    if poly.area <= 0 or len(poly.exterior.coords) > 5:
        return False
    with np.errstate(all="ignore"):
        return poly.area >= 0.98 * poly.minimum_rotated_rectangle.area


def zones_from_svg(svg_path: str) -> dict[str, list[Polygon]]:
    """Fill hex -> polygons (SVG units), glyph outlines included (callers filter by area)."""
    by_fill: dict[str, list[Polygon]] = defaultdict(list)
    text = unwrap_blend_groups(pathlib.Path(svg_path).read_text())
    for el in SVG.parse(io.StringIO(text), reify=True).elements():
        if not isinstance(el, Shape):
            continue
        fill = _hex(getattr(el, "fill", None))
        if fill is None or fill == "#ffffff":
            continue
        for ring in _rings(el):
            poly = Polygon(ring)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty or poly.area <= 0 or _is_swatch(poly):
                continue
            by_fill[fill].extend(poly.geoms if poly.geom_type == "MultiPolygon" else [poly])
    return by_fill


def build_trace(by_fill, legend, scale, min_area, category, source, keep_unnamed):
    zones = []
    for fill, polys in sorted(by_fill.items()):
        polys = [p for p in polys if p.area >= min_area]
        if not polys:
            continue
        entry = legend.get(fill)
        if entry is None and not keep_unnamed:
            continue
        info = {"name": entry} if isinstance(entry, str) else dict(entry or {"name": f"zone-{fill[1:]}"})
        name = info.pop("name")
        info.setdefault("category", category)
        polys.sort(key=lambda p: (round(p.centroid.x), round(p.centroid.y)))
        for i, poly in enumerate(polys, 1):
            ring = [[round(x * scale, 1), round(y * scale, 1)] for x, y in list(poly.exterior.coords)[:-1]]
            slug = re.sub(r"[^0-9a-z]+", "", fill)
            zones.append({"id": f"zone-{slug}-{i}", "name": name if len(polys) == 1 else f"{name} {i}",
                          "source": source, **info, "px": ring})
    return zones


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("svg")
    ap.add_argument("out", nargs="?")
    ap.add_argument("--image", help="the rendered image the traced pixels refer to: the scale is its width / the SVG's")
    ap.add_argument("--scale", type=float, default=1.0, help="image pixels per SVG unit (default 1, or from --image)")
    ap.add_argument("--legend", help="JSON: fill hex -> name or {name, category, ...}")
    ap.add_argument("--list", action="store_true", help="print the fills found and stop")
    ap.add_argument("--min-area", type=float, default=150.0, help="drop shapes smaller than this (SVG units²)")
    ap.add_argument("--category", default="seating")
    ap.add_argument("--source", default="s1")
    ap.add_argument("--keep-unnamed", action="store_true")
    a = ap.parse_args(argv)

    by_fill = zones_from_svg(a.svg)
    if a.list:
        for fill, polys in sorted(by_fill.items(), key=lambda kv: -sum(p.area for p in kv[1])):
            big = [p for p in polys if p.area >= a.min_area]
            if big:
                print(f"{fill}  shapes={len(big):3d}  area={sum(p.area for p in big):9.0f}")
        return 0
    if not a.out:
        ap.error("out is required unless --list")
    legend = json.loads(pathlib.Path(a.legend).read_text()) if a.legend else {}
    scale = a.scale
    if a.image:
        from PIL import Image
        with Image.open(a.image) as im:
            scale = im.width / float(SVG.parse(a.svg).width)
        print(f"scale {scale:.4f} image px per SVG unit")
    zones = build_trace(by_fill, legend, scale, a.min_area, a.category, a.source, a.keep_unnamed)
    pathlib.Path(a.out).write_text(json.dumps(zones, ensure_ascii=False, indent=1))
    print(f"{len(zones)} zones -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
