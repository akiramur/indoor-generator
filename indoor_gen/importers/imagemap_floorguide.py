"""Import tenant shapes from floor-guide pages drawn as an HTML image map.

Many shopping-centre sites show each floor
as a raster image with `<img usemap>` and clickable `<area>` hotspots, one per
tenant. The hotspots are the tenant outlines in image pixels.

Georeferencing: the building outline is the largest opaque region of the
image (the background is transparent / white); it is fitted onto the OSM
footprint with the map's "up" direction given by the caller. The tenant
hotspots go through the same transform.
"""

from __future__ import annotations

import io
import re
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from shapely.geometry import MultiPolygon, Point, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .. import align, georef
from .common import LOW_FIT, floor_level

USER_AGENT = "Mozilla/5.0 (indoor-generator)"


class _ImageMapParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images: list[dict] = []  # <img usemap>
        self.maps: dict[str, list[dict]] = {}
        self._map: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "img" and a.get("usemap"):
            self.images.append(a)
        elif tag == "map":
            self._map = a.get("name") or a.get("id")
            self.maps.setdefault(self._map, [])
        elif tag == "area" and self._map is not None:
            self.maps[self._map].append(a)

    def handle_endtag(self, tag):
        if tag == "map":
            self._map = None


def area_geometry(shape: str, coords: str) -> BaseGeometry | None:
    nums = [float(n) for n in re.split(r"[\s,]+", coords.strip()) if n]
    shape = (shape or "rect").lower()
    if shape in ("rect", "rectangle") and len(nums) >= 4:
        x1, y1, x2, y2 = nums[:4]
        return box(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
    if shape in ("circle", "circ") and len(nums) >= 3:
        return Point(nums[0], nums[1]).buffer(nums[2])
    if shape in ("poly", "polygon") and len(nums) >= 6:
        g = Polygon(list(zip(nums[0::2], nums[1::2]))).buffer(0)
        return g if not g.is_empty else None
    return None


def _get(url: str, dest: Path, refresh: bool) -> Path:
    if dest.exists() and not refresh:
        return dest
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
    resp.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)
    return dest


def _page_title(url: str, cache_dir: Path, refresh: bool) -> str | None:
    """Name of a tenant whose hotspot has no alt text, from its own page's <h1>/<title>."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", urlparse(url).path.strip("/")) or "index"
    try:
        html = _get(url, cache_dir / "pages" / f"{slug}.html", refresh).read_text(encoding="utf-8", errors="replace")
    except requests.RequestException:
        return None
    for pat in (r"<h1[^>]*>(.*?)</h1>", r"<title[^>]*>(.*?)</title>"):
        m = re.search(pat, html, re.S | re.I)
        if m:
            text = unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
            text = re.split(r"\s*[—|｜–-]\s*", text)[0].strip()
            if text:
                return text
    return None


def fetch_floor(page_url: str, name: str, cache_dir: Path, refresh: bool = False) -> dict:
    """Download one floor page and its map image; return tenants with pixel outlines."""
    html_path = _get(page_url, cache_dir / f"{name}.html", refresh)
    p = _ImageMapParser()
    p.feed(html_path.read_text(encoding="utf-8", errors="replace"))
    if not p.images:
        raise ValueError(f"{page_url}: no <img usemap> on the page")
    img = max(p.images, key=lambda a: len(p.maps.get(a["usemap"].lstrip("#"), [])))
    image_url = urljoin(page_url, img["src"])
    image_path = _get(image_url, cache_dir / f"{name}{Path(urlparse(image_url).path).suffix or '.png'}", refresh)
    tenants = []
    for a in p.maps.get(img["usemap"].lstrip("#"), []):
        geom = area_geometry(a.get("shape", "rect"), a.get("coords", ""))
        if geom is None or geom.area < 4:
            continue
        href = urljoin(page_url, a["href"]) if a.get("href") else None
        label = (a.get("alt") or "").strip() or (href and _page_title(href, cache_dir, refresh)) or "テナント"
        tenants.append({"name": label, "href": href, "pin": geom})
    for i, t in enumerate(tenants, 1):
        t["no"] = i
    return {"name": name, "page": page_url, "image": image_path, "image_url": image_url,
            "display_size": (float(img.get("width") or 0), float(img.get("height") or 0)), "tenants": tenants}


def image_outline(path: Path, display_size: tuple[float, float]) -> tuple[BaseGeometry, tuple[float, float]]:
    """Largest opaque, non-white region of the image, in the page's display pixels."""
    return align.image_outline(path, display_size)


def analyze_floor(floor: dict, footprint_local: BaseGeometry, map_up: str, exclude: set[int]) -> dict:
    outline, canvas = image_outline(floor["image"], floor["display_size"])
    tenants = [t for t in floor["tenants"] if t["no"] not in exclude]
    turns = georef.MAP_UP_TURNS[map_up]
    t, fit = georef.fit(outline, footprint_local, base=georef.FLIP_Y, orientations=[(turns, False)])
    rep = {"floor": floor["name"], "tenants": len(floor["tenants"]), "excluded": sorted(exclude),
           "fit_map_to_footprint": round(fit, 3), "warnings": []}
    return {"floor": floor, "outline": outline, "canvas": canvas, "tenants": tenants,
            "t_svg_local": t, "fit2": fit, "report": rep}


def build_plan(a: dict, footprint_local: BaseGeometry, to_wgs84, map_up: str,
               source_id: str | None) -> tuple[dict, dict]:
    """Return (plan FeatureCollection in WGS84, report)."""
    from shapely.geometry import mapping

    floor, rep = a["floor"], a["report"]
    level = floor_level(floor["name"])
    t = a["t_svg_local"]
    # Hotspots are hand-drawn and sometimes overshoot; keep them inside the drawn building.
    drawn = georef.apply(a["outline"], t)
    features, outside, total, seen = [], 0.0, 0.0, {}
    for tn in a["tenants"]:
        g = georef.apply(tn["pin"], t)
        total += g.area
        outside += g.difference(footprint_local).area
        g = g.intersection(drawn)
        g = MultiPolygon([p for p in getattr(g, "geoms", [g]) if isinstance(p, Polygon) and p.area > 1]) \
            if g.geom_type != "Polygon" else g
        if g.is_empty:
            continue
        key = tn["href"] or tn["name"]
        seen[key] = seen.get(key, 0) + 1
        rid = f"L{level}-{_slug(tn)}" + (f"-{seen[key]}" if seen[key] > 1 else "")
        props = {"id": rid, "name": tn["name"], "map_no": tn["no"], "confidence": "sourced"}
        if tn["href"]:
            props["url"] = tn["href"]
        if source_id:
            props["source"] = source_id
        features.append({"type": "Feature", "properties": props, "geometry": mapping(to_wgs84(g))})
    rep["imported"] = len(features)
    rep["tenant_area_m2"] = round(total, 1)
    rep["outside_footprint_pct"] = round(100 * outside / total, 1) if total else 0.0
    if rep["outside_footprint_pct"] > 10:
        rep["warnings"].append("more than 10% of tenant area falls outside the footprint")
    w, h = a["canvas"]
    corners = [georef.apply(Point(x, y), t) for x, y in ((0, 0), (w, 0), (w, h), (0, h))]
    basemap = {"url": floor["image_url"],
               "corners": [[round(c, 7) for c in to_wgs84(p).coords[0]] for p in corners]}
    return {"type": "FeatureCollection",
            "metadata": {"source": floor["page"], "floor": floor["name"], "map_up": map_up, "basemap": basemap,
                         "geometry_source": "imagemap", "georef": {"method": "outline_fit"}},
            "features": features}, rep


def _slug(tenant: dict) -> str:
    if tenant["href"]:
        last = urlparse(tenant["href"]).path.rstrip("/").rsplit("/", 1)[-1]
        if re.fullmatch(r"[A-Za-z0-9_-]+", last or ""):
            return last.lower()
    return f"t{tenant['no']:02d}"
