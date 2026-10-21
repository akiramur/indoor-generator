"""Seat blocks (rows x seat numbers, in a hall's own frame) -> a plan GeoJSON of point seats.

    .venv/bin/python tools/gen_seat_plan.py configs/plans/<slug>/seat-spec.json configs/plans/<slug>/<floor>.geojson \
        [--hall-name ホールC] [--stage-name 舞台] [--source s3]

The spec holds the hall's origin (stage front, centre), the bearing of stage -> audience and the blocks. Frame: u =
metres to the audience's right (looking at the stage), v = metres from the stage front toward the rear. A block is
either
  - a grid: `seats: [first, last]`, `seat_pitch`, `u_center` and `row_groups: [{rows: [a, b], v0, pitch}]`, rows
    straight across the hall; or
  - explicit rows: `rows: [{row, from: [u, v], to: [u, v], numbers: [a, b], not_fixed?: [numbers]}]`, each row a straight
    line of seats from `from` (number a) to `to` (number b), evenly spaced; the seats turn with the row (a fan).
Each seat is a Point with `icon: seat`, `section` / `row` / `number`, `facing_deg` (toward the stage); each row also gets
a `row-label` point at its left / right end (per block `row_labels`, both by default); the stage and the seating area
are rooms (`hall`; `stage_room: false` / `seating_room: false` in the spec leave one out, e.g. where the stalls are an
open void below a balcony). A block's `tier` (1 / 2 / 3) is put on its seats. `block_rooms: true` draws the area under
each block (its seats' convex hull + a margin, tinted by tier) as a room, so a balcony without a drawn floor is not just
seats on the common floor; `block_rooms: "merged"` makes it one room for the whole deck (named `area_name`, default
"<tier>階席").
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from shapely.geometry import MultiPoint, Polygon
from shapely.ops import unary_union

M_PER_DEG_LAT = 110574.0
ROW_LABEL_GAP = 0.75   # metres from the end seat's centre to the row number
BLOCK_ROOM_MARGIN = 0.8   # metres the area under a block of seats reaches beyond its outer seats
MERGE_GAP_MAX = 8.0   # metres: the widest gap between blocks that "merged" closes to make one piece
MERGE_GAP = 4.0   # metres: "merged" always closes gaps up to twice this (aisles, steps in the outline), and rounds corners by it / 3
TIER_AREA_COLORS = {1: "#cfe0f5", 2: "#f7dcae", 3: "#c3e6d0"}   # light tints of the seating chart's blue / orange / green


def _east_north(bearing_deg, u, v):
    """Metres east / north of a hall-frame vector (u, v). v points along the stage -> audience bearing; the audience
    faces the stage (bearing + 180), so its right hand is bearing - 90."""
    b = math.radians(bearing_deg)
    return (v * math.sin(b) + u * math.sin(b - math.pi / 2),
            v * math.cos(b) + u * math.cos(b - math.pi / 2))


def to_lonlat(origin, bearing_deg, u, v):
    """Hall frame -> lon/lat."""
    lon0, lat0 = origin
    east, north = _east_north(bearing_deg, u, v)
    kx = 111320.0 * math.cos(math.radians(lat0))
    return [round(lon0 + east / kx, 8), round(lat0 + north / M_PER_DEG_LAT, 8)]


def facing_bearing(bearing_deg, du, dv):
    """Compass bearing a seat faces (toward the stage) when its row runs along the hall-frame direction (du, dv): the
    row turned a quarter turn toward -v. A straight row (1, 0) faces the stage, bearing + 180."""
    east, north = _east_north(bearing_deg, dv, -du)
    return round(math.degrees(math.atan2(east, north)) % 360, 2)


def rect(origin, bearing, u0, u1, v0, v1):
    pts = [(u0, v0), (u1, v0), (u1, v1), (u0, v1), (u0, v0)]
    return {"type": "Polygon", "coordinates": [[to_lonlat(origin, bearing, u, v) for u, v in pts]]}


def block_rows(block: dict):
    """Yield (row, [(number, u, v)], (du, dv) unit direction of the row, not-fixed seat numbers) for a block."""
    if "rows" in block:
        for r in block["rows"]:
            a, b = r["numbers"]
            n = b - a + 1
            (u0, v0), (u1, v1) = r["from"], r["to"]
            length = math.hypot(u1 - u0, v1 - v0) or 1.0
            t = lambda k: k / (n - 1) if n > 1 else 0.0
            seats = [(a + k, u0 + (u1 - u0) * t(k), v0 + (v1 - v0) * t(k)) for k in range(n)]
            yield r["row"], seats, ((u1 - u0) / length, (v1 - v0) / length), set(r.get("not_fixed", []))
        return
    first, last = block["seats"]
    count = last - first + 1
    for group in block["row_groups"]:
        r0, r1 = group["rows"]
        for row in range(r0, r1 + 1):
            v = group["v0"] + (row - r0) * group["pitch"]
            seats = [(first + k, block.get("u_center", 0.0) + (k - (count - 1) / 2) * block["seat_pitch"], v)
                     for k in range(count)]
            yield row, seats, (1.0, 0.0), set()


def merged_room(origin, bearing, hulls, name, src, merge_gap=MERGE_GAP):
    """One room from the blocks' areas: their union, closed (grown then shrunk by the same distance) by `merge_gap`, or
    by the least gap that makes it one piece if that is more (aisles, steps in the outline, the gap to a side balcony;
    the void in the middle of a U stays open), then opened a little (rounds the corners), no holes."""
    union = unary_union([h for _, h in hulls])
    tier = min(t for t, _ in hulls)
    grow = lambda g, shape: shape.buffer(g, join_style=1, quad_segs=8).buffer(-g, join_style=1, quad_segs=8)
    gap = merge_gap
    shape = grow(gap, union)
    while shape.geom_type != "Polygon" and gap <= MERGE_GAP_MAX:
        gap += 0.25
        shape = grow(gap, union)
    r = merge_gap / 3
    shape = grow(r, shape.buffer(-r, join_style=1, quad_segs=8).buffer(r, join_style=1, quad_segs=8)) if r else shape
    parts = [shape] if shape.geom_type == "Polygon" else list(shape.geoms)
    rings = [[to_lonlat(origin, bearing, u, v) for u, v in Polygon(part.exterior).exterior.coords] for part in parts]
    geometry = ({"type": "Polygon", "coordinates": [rings[0]]} if len(rings) == 1
                else {"type": "MultiPolygon", "coordinates": [[r] for r in rings]})
    # the id is unique across floors
    props = {"id": f"area-tier{tier}", "name": name or f"{tier}階席", "category": "hall", "color": TIER_AREA_COLORS.get(tier),
             "confidence": "estimated", **src}
    return {"type": "Feature", "properties": props, "geometry": geometry}


def build(spec: dict, hall: str, stage_name: str, source: str | None) -> dict:
    origin, bearing = spec["origin"], spec["audience_bearing_deg"]
    stage = spec["stage"]
    half = stage["width"] / 2
    src = {"source": source} if source else {}
    feats = []
    if spec.get("stage_room", True):
        feats.append({"type": "Feature", "properties": {"id": "stage", "name": f"{hall} {stage_name}", "category": "hall",
                                                         "confidence": "estimated", **src},
                      "geometry": rect(origin, bearing, -half, half, -stage["depth"], 0.0)})
    all_u, all_v = [], []
    hulls = []   # (tier, shapely polygon in the hall frame) per block, for `block_rooms: "merged"`
    for block in spec["blocks"]:
        block_pts, block_rows_seen = [], []
        for row, seats, (du, dv), not_fixed in block_rows(block):
            facing = facing_bearing(bearing, du, dv)
            block_rows_seen.append(row)
            for number, u, v in seats:
                all_u.append(u)
                all_v.append(v)
                block_pts.append((u, v))
                props = {"id": f"seat-{block['id']}-{row:02d}-{number}",
                         "name": f"{block['section']} {row}列{number}番", "icon": "seat",
                         "section": block["section"], "row": row, "number": number,
                         # the direction the seat faces (toward the stage): the preview turns the seat's rectangle by it
                         "facing_deg": facing, "confidence": "estimated", **src}
                if "tier" in block:
                    props["tier"] = block["tier"]   # 1 / 2 / 3 階席: the preview colours the seat like the seating chart
                if number in not_fixed:
                    props["fixed"] = False   # not a permanent seat (a wheelchair space)
                feats.append({"type": "Feature", "properties": props,
                              "geometry": {"type": "Point", "coordinates": to_lonlat(origin, bearing, u, v)}})
            # the row number beside each end of the row (left / right as the audience sees it), as a point of its own
            for edge, (_, u, v), sign in (("left", seats[0], -1), ("right", seats[-1], 1)):
                if edge not in block.get("row_labels", ["left", "right"]):
                    continue
                lu, lv = u + sign * du * ROW_LABEL_GAP, v + sign * dv * ROW_LABEL_GAP
                lab = {"id": f"rowlabel-{block['id']}-{row:02d}-{edge}", "name": f"{block['section']} {row}列",
                       "icon": "row-label", "section": block["section"], "row": row, "row_label": edge,
                       "facing_deg": facing, "confidence": "estimated", **src}
                feats.append({"type": "Feature", "properties": lab,
                              "geometry": {"type": "Point", "coordinates": to_lonlat(origin, bearing, lu, lv)}})
        hull = MultiPoint(block_pts).convex_hull.buffer(BLOCK_ROOM_MARGIN, join_style=1, quad_segs=4)
        hulls.append((block.get("tier", 1), hull))
        if spec.get("block_rooms") is True:   # the area under a block: its seats' convex hull plus a margin
            ring = [to_lonlat(origin, bearing, u, v) for u, v in hull.exterior.coords]
            r0, r1 = min(block_rows_seen), max(block_rows_seen)
            props = {"id": f"area-{block['id']}", "name": f"{block['section']} {r0}〜{r1}列", "category": "hall",
                     "color": block.get("color") or TIER_AREA_COLORS.get(block.get("tier", 1)),
                     "confidence": "estimated", **src}
            feats.append({"type": "Feature", "properties": props, "geometry": {"type": "Polygon", "coordinates": [ring]}})
    if spec.get("block_rooms") == "merged":   # one room for the whole deck: the blocks joined across aisles and gaps
        feats.append(merged_room(origin, bearing, hulls, spec.get("area_name"), src, spec.get("merge_gap", MERGE_GAP)))
    # the seating area: the seats' extent plus a margin
    m = 1.0
    if spec.get("seating_room", True):
        feats.insert(1 if spec.get("stage_room", True) else 0, {
            "type": "Feature", "properties": {"id": "seating", "name": f"{hall} 客席", "category": "hall",
                                              "confidence": "estimated", **src},
            "geometry": rect(origin, bearing, min(all_u) - m, max(all_u) + m, 0.0, max(all_v) + m)})
    return {"type": "FeatureCollection",
            "metadata": {"geometry_source": "drawn", "georef": {"method": "direct"}},
            "features": feats}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec")
    ap.add_argument("out")
    ap.add_argument("--hall-name", default="ホールC")
    ap.add_argument("--stage-name", default="舞台")
    ap.add_argument("--source", default=None, help="provenance source id")
    a = ap.parse_args()
    spec = json.loads(Path(a.spec).read_text(encoding="utf-8"))
    fc = build(spec, a.hall_name, a.stage_name, a.source)
    Path(a.out).write_text(json.dumps(fc, ensure_ascii=False, indent=1), encoding="utf-8")
    seats = sum(1 for f in fc["features"] if f["properties"].get("icon") == "seat")
    labels = sum(1 for f in fc["features"] if f["properties"].get("icon") == "row-label")
    print(f"wrote {a.out}: {seats} seats, {labels} row labels, {len(fc['features']) - seats - labels} rooms")


if __name__ == "__main__":
    main()
