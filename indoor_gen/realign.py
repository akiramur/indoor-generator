"""Move a building's plans and floor maps together when its footprint is replaced (`fit`, a library function: no command calls it now) or by hand (`main.py nudge-map`).

One similarity transform (scale, rotation, shift) is fitted for the whole building: the union of every floor's plan
rooms is moved onto the new footprint (best overlap). The same transform is applied to every plan GeoJSON, to the
corners and point pairs of every floor map, so rooms and maps keep matching each other.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from . import georef
from .geo_utils import LocalFrame
from .room_edit import _backup

CLOSE_M = 6.0      # aisles between rooms are closed before fitting: the plan's rooms alone are not the whole floor
MIN_GAIN = 0.005   # the fit must improve the overlap by at least this, or nothing is moved


def _on_level(f: dict, level: int | None) -> bool:
    return level is None or f.get("level") == level or level in f.get("levels", [])


def _plan_files(raw: dict, base: Path, level: int | None = None) -> list[Path]:
    seen: list[Path] = []
    for f in raw.get("floors", []):
        if not _on_level(f, level):
            continue
        p = f.get("plan")
        if isinstance(p, str) and (base / p) not in seen and (base / p).exists():
            seen.append(base / p)
    return seen


def _polygons(path: Path) -> list[BaseGeometry]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    return [shape(f["geometry"]) for f in obj.get("features", [])
            if f.get("geometry") and f["geometry"]["type"] in ("Polygon", "MultiPolygon")]


def _walk(coords, fn):
    if coords and isinstance(coords[0], (int, float)):
        return fn(coords)
    return [_walk(c, fn) for c in coords]


def _geometries(obj: dict) -> list[dict]:
    """The geometry dicts of a GeoJSON geometry / Feature / FeatureCollection (edited in place)."""
    if obj.get("type") == "FeatureCollection":
        return [g for f in obj.get("features", []) for g in _geometries(f)]
    if obj.get("type") == "Feature":
        return _geometries(obj["geometry"]) if obj.get("geometry") else []
    return [obj] if obj.get("coordinates") is not None else []


def _follows_map(obj: dict) -> bool:
    """Was this GeoJSON (an outline) drawn from a floor map? `properties.follows_map` on the Feature (or the first one)."""
    if obj.get("type") == "FeatureCollection":
        obj = (obj.get("features") or [{}])[0]
    return bool((obj.get("properties") or {}).get("follows_map"))


def fit(raw: dict, base: Path, new_fp: BaseGeometry) -> dict:
    """The transform that puts the plans onto `new_fp`: {"t", "frame", "before", "after", "scale", "rotation_deg", "shift_m"}."""
    b = raw["building"]
    frame = LocalFrame(new_fp, b.get("crs", "auto"), b.get("orientation", "auto"))
    polys = [p for f in _plan_files(raw, base) for p in _polygons(f)]
    if not polys:
        return {"t": georef.IDENTITY, "frame": frame, "before": None, "after": None, "moved": False}
    src = frame.to_local(unary_union([p.buffer(0) for p in polys])).buffer(CLOSE_M).buffer(-CLOSE_M)
    target = frame.to_local(new_fp)
    before = georef.iou(src, target)
    t, after = georef.refine(src, target, georef.IDENTITY, rounds=4)
    moved = after >= before + MIN_GAIN
    a, bb, d, e, xo, yo = t
    c = src.centroid
    c2 = georef.apply(c, t)
    return {"t": t if moved else georef.IDENTITY, "frame": frame, "moved": moved,
            "before": round(before, 3), "after": round(after, 3), "scale": round(math.hypot(a, d), 4),
            "rotation_deg": round(math.degrees(math.atan2(d, a)), 2),
            "shift_m": round(math.hypot(c2.x - c.x, c2.y - c.y), 2)}


def manual(raw: dict, base: Path, footprint: BaseGeometry, rotate_deg: float = 0.0, scale: float = 1.0,
           east_m: float = 0.0, north_m: float = 0.0) -> dict:
    """A hand-set transform in the shape `apply` takes: rotate (counter-clockwise on the map, about the footprint's
    centre), scale, then shift east / north in metres."""
    b = raw["building"]
    frame = LocalFrame(footprint, b.get("crs", "auto"), b.get("orientation", "auto"))
    c = frame.to_local(footprint).centroid
    o = frame.to_local(Point(footprint.centroid.x, footprint.centroid.y))
    e = frame.to_local(Point(footprint.centroid.x + 1e-5, footprint.centroid.y))     # local vector of "east"
    n = frame.to_local(Point(footprint.centroid.x, footprint.centroid.y + 1e-5))
    ue = ((e.x - o.x), (e.y - o.y)); un = ((n.x - o.x), (n.y - o.y))
    ne, nn = math.hypot(*ue), math.hypot(*un)
    shift = (east_m * ue[0] / ne + north_m * un[0] / nn, east_m * ue[1] / ne + north_m * un[1] / nn)
    th = math.radians(rotate_deg)
    a, d = scale * math.cos(th), scale * math.sin(th)
    t = [a, -d, d, a, c.x - (a * c.x - d * c.y) + shift[0], c.y - (d * c.x + a * c.y) + shift[1]]
    return {"t": t, "frame": frame, "moved": True}


def apply(raw: dict, base: Path, result: dict, out_dir: Path | None = None, level: int | None = None) -> list[str]:
    """Write the transform onto the plan files and the floor maps in `raw` (mutated). Returns what was changed.
    `level`: move only that floor's plan, outline and map (the footprint and the other floors stay)."""
    if not result["moved"]:
        return []
    frame, t = result["frame"], result["t"]

    def move(c):   # one [lon, lat(, z)] position
        p = georef.apply(frame.to_local(Point(c[0], c[1])), t)
        q = frame.to_wgs84(p).coords[0]
        return [round(q[0], 8), round(q[1], 8), *c[2:]]

    changed: list[str] = []
    # A footprint drawn from the maps (`building.footprint_follows_map`) and floor outlines drawn from them
    # (a Feature property `follows_map: true`) move with the maps. A real footprint, or an OSM part used as an outline, stays.
    src = raw.get("building", {}).get("source") or {}
    whole = bool(raw.get("building", {}).get("footprint_follows_map"))
    files = []
    if level is None and whole and src.get("type") == "geojson" and src.get("path"):
        files.append((base / src["path"], True))
    files += [(base / f["outline"], whole) for f in raw.get("floors", [])
              if isinstance(f.get("outline"), str) and _on_level(f, level)]
    seen = set()
    for path, always in files:
        if path in seen or not path.exists():
            continue
        seen.add(path)
        obj = json.loads(path.read_text(encoding="utf-8"))
        if not (always or _follows_map(obj)):
            continue
        for g in _geometries(obj):
            g["coordinates"] = _walk(g["coordinates"], move)
        _backup(path, base)
        path.write_text(json.dumps(obj, ensure_ascii=False) + "\n", encoding="utf-8")
        changed.append(str(path.relative_to(base)))
    for path in _plan_files(raw, base, level):
        obj = json.loads(path.read_text(encoding="utf-8"))
        for f in obj.get("features", []):
            if f.get("geometry"):
                f["geometry"]["coordinates"] = _walk(f["geometry"]["coordinates"], move)
        pm = (obj.get("metadata") or {}).get("basemap")   # a site importer keeps its floor map's corners in the plan
        if pm and pm.get("corners"):
            pm["corners"] = [move(c) for c in pm["corners"]]
        _backup(path, base)
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        changed.append(str(path.relative_to(base)))

    def move7(c):
        m = move(c)
        return [round(m[0], 7), round(m[1], 7)]

    for f in raw.get("floors", []):
        bm = f.get("basemap")
        if not bm or not _on_level(f, level):
            continue
        for key in ("corners", "auto_corners"):
            if bm.get(key):
                bm[key] = [move7(c) for c in bm[key]]
        al = bm.get("alignment") or {}
        for pr in al.get("pairs", []):
            pr["lnglat"] = move7(pr["lnglat"])
        if al.get("rms_m") is not None:   # a similarity transform scales the residuals with the map
            al["rms_m"] = round(al["rms_m"] * math.hypot(t[0], t[3]), 2)
        changed.append(f"floor {f.get('level')} map")
    apath = out_dir / "alignment.json" if out_dir else None
    if apath and apath.exists():
        al = json.loads(apath.read_text(encoding="utf-8"))
        for pr in al.get("pairs", []) if level is None or al.get("level") == level else []:
            pr["lnglat"] = move7(pr["lnglat"])
        apath.write_text(json.dumps(al, ensure_ascii=False), encoding="utf-8")
    return changed
