"""Compare two builds of the same building, e.g. one made with a cheaper model.

Reads each output directory's indoor.geojson and cost.json and reports, side by side:
- cost: estimated USD, turns, output tokens (cost.json);
- evidence: room count and the share of room area that is sourced vs estimated, and where room
  shapes came from (`geometry_source`: ratio strips, imported vectors, traced plans, ...);
- floor-map alignment: residual (rms_m) and number of point pairs of each aligned basemap;
- agreement per common floor: the share of floor area given the same category by both builds,
  and how many of A's rooms have a counterpart in B (best overlap, IoU >= 0.5) and with the same name.
A is the reference (e.g. the Opus build); B is the candidate.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from shapely.geometry import shape
from shapely.ops import unary_union

from indoor_gen.generate_geojson import is_area_room
from indoor_gen.geo_utils import LocalFrame

MATCH_IOU = 0.5


def load(out_dir: Path) -> dict:
    fc = json.loads((out_dir / "indoor.geojson").read_text(encoding="utf-8"))
    cost_path = out_dir / "cost.json"
    cost = json.loads(cost_path.read_text(encoding="utf-8")) if cost_path.exists() else None
    return {"dir": str(out_dir), "fc": fc, "cost": cost}


def _rooms(fc: dict, frame: LocalFrame) -> dict[int, list[dict]]:
    by_level = defaultdict(list)
    for f in fc["features"]:
        p = f["properties"]
        if is_area_room(p):
            by_level[p["level"]].append({**p, "geom": frame.to_local(shape(f["geometry"])).buffer(0)})
    return by_level


def _name(s: str) -> str:
    return re.sub(r"[\s・（）()\-]", "", unicodedata.normalize("NFKC", s or "")).lower()


def _iou(a, b) -> float:
    union = a.union(b).area
    return a.intersection(b).area / union if union else 0.0


def summary(build: dict, rooms: dict[int, list[dict]]) -> dict:
    all_rooms = [r for rs in rooms.values() for r in rs]
    area = sum(r["area_m2"] for r in all_rooms) or 1
    sourced = sum(r["area_m2"] for r in all_rooms if r.get("confidence") == "sourced")
    cost = build["cost"] or {}
    tokens = Counter()
    for t in (cost.get("models") or {}).values():
        tokens.update(t)
    return {
        "dir": build["dir"],
        "cost_usd": cost.get("usd"),
        "turns": cost.get("turns"),
        "models": sorted(cost.get("models") or {}),
        "output_tokens": tokens["output"],
        "levels": len(rooms),
        "rooms": len(all_rooms),
        "sourced_area_pct": round(100 * sourced / area, 1),
        "geometry_source": dict(Counter(r.get("geometry_source") or "ratio" for r in all_rooms).most_common()),
        "basemaps": len(build["fc"]["metadata"].get("basemaps", [])),
        "aligned_basemaps": {bm["level"]: {k: bm["alignment"].get(k) for k in ("mode", "rms_m")}
                             | {"pairs": len(bm["alignment"].get("pairs") or [])}
                             for bm in build["fc"]["metadata"].get("basemaps", []) if bm.get("alignment")},
    }


def floor_agreement(a: list[dict], b: list[dict]) -> dict:
    cats = {r["category"] for r in a + b}
    union = unary_union([r["geom"] for r in a + b])
    same = sum(unary_union([r["geom"] for r in a if r["category"] == c])
               .intersection(unary_union([r["geom"] for r in b if r["category"] == c])).area for c in cats)
    matched, same_name, missing = 0, 0, []
    for ra in a:
        best = max(b, key=lambda rb: _iou(ra["geom"], rb["geom"]), default=None)
        if best is not None and _iou(ra["geom"], best["geom"]) >= MATCH_IOU:
            matched += 1
            same_name += _name(ra["name"]) == _name(best["name"])
        else:
            missing.append(ra["name"])
    return {"rooms_a": len(a), "rooms_b": len(b),
            "category_agreement_pct": round(100 * same / union.area, 1) if union.area else None,
            "matched_rooms": matched, "matched_same_name": same_name,
            "unmatched_in_b": missing[:8] + (["…"] if len(missing) > 8 else [])}


def compare(dir_a: Path, dir_b: Path) -> dict:
    a, b = load(dir_a), load(dir_b)
    md = a["fc"]["metadata"]
    frame = LocalFrame(shape(md["footprint"]), md.get("crs", "auto"))  # same building: one frame for both
    rooms_a, rooms_b = _rooms(a["fc"], frame), _rooms(b["fc"], frame)
    floors, weighted = {}, []
    for level in sorted(set(rooms_a) & set(rooms_b), reverse=True):
        agreement = floor_agreement(rooms_a[level], rooms_b[level])
        floors[rooms_a[level][0]["floor_name"]] = agreement
        if agreement["category_agreement_pct"] is not None:
            weighted.append((agreement["category_agreement_pct"], sum(r["geom"].area for r in rooms_a[level])))
    return {
        "building": md.get("building"),
        "a": summary(a, rooms_a),
        "b": summary(b, rooms_b),
        "levels_only_in_a": sorted(set(rooms_a) - set(rooms_b)),
        "levels_only_in_b": sorted(set(rooms_b) - set(rooms_a)),
        "category_agreement_pct": round(sum(p * w for p, w in weighted) / sum(w for _, w in weighted), 1)
        if weighted else None,
        "floors": floors,
    }

