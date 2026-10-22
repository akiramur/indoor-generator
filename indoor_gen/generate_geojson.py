"""Split a building footprint into rooms per floor and emit GeoJSON."""

from __future__ import annotations

import math
import hashlib
import json
import re
from pathlib import Path
from datetime import datetime, timezone

from shapely.geometry import MultiPolygon, Polygon, box, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.validation import make_valid

from .config import CATEGORY_COLORS, UNASSIGNED_COLOR, BuildingConfig, Floor, Room
from .geo_utils import LocalFrame

MIN_PART_AREA = 1.0  # m², slivers below this are dropped
SLAB_THICKNESS = 0.15
FLAT_HEIGHT = 0.2  # metres above the slab for rooms drawn flat
COORD_PRECISION = 7
SEARCH_ITERATIONS = 60


def _polygonal(geom: BaseGeometry) -> BaseGeometry:
    """Keep only polygon parts above MIN_PART_AREA."""
    geom = make_valid(geom)
    parts = []
    if isinstance(geom, Polygon):
        parts = [geom]
    elif hasattr(geom, "geoms"):
        for g in geom.geoms:
            if isinstance(g, (Polygon, MultiPolygon)):
                parts.extend([g] if isinstance(g, Polygon) else list(g.geoms))
    parts = [p for p in parts if p.area >= MIN_PART_AREA]
    if not parts:
        return Polygon()
    return parts[0] if len(parts) == 1 else MultiPolygon(parts)


def cut_from_side(region: BaseGeometry, side: str, target_area: float) -> tuple[BaseGeometry, BaseGeometry]:
    """Cut `target_area` m² off `region` starting from `side`.

    The cut line is parallel to the local axes; its position is found by
    bisection on the clipped area, so ratios stay exact for any shape.
    Returns (piece, remainder).
    """
    if region.is_empty:
        return Polygon(), Polygon()
    if target_area >= region.area - 1e-9:
        return region, Polygon()
    minx, miny, maxx, maxy = region.bounds
    pad = 1.0

    def clip(t: float) -> BaseGeometry:
        if side == "west":
            b = box(minx - pad, miny - pad, t, maxy + pad)
        elif side == "east":
            b = box(t, miny - pad, maxx + pad, maxy + pad)
        elif side == "south":
            b = box(minx - pad, miny - pad, maxx + pad, t)
        elif side == "north":
            b = box(minx - pad, t, maxx + pad, maxy + pad)
        else:
            raise ValueError(f"unknown side {side}")
        return region.intersection(b)

    lo, hi = (minx, maxx) if side in ("west", "east") else (miny, maxy)
    grows_with_t = side in ("west", "south")
    for _ in range(SEARCH_ITERATIONS):
        mid = (lo + hi) / 2
        a = clip(mid).area
        if (a < target_area) == grows_with_t:
            lo = mid
        else:
            hi = mid
    piece = clip((lo + hi) / 2)
    return _polygonal(piece), _polygonal(region.difference(piece))


def subdivide(region: BaseGeometry, count: int, axis: str = "long") -> list[BaseGeometry]:
    """Split region into `count` equal-area parts laid out along an axis."""
    minx, miny, maxx, maxy = region.bounds
    x_is_long = (maxx - minx) >= (maxy - miny)
    along_x = x_is_long if axis == "long" else not x_is_long
    side = "west" if along_x else "south"
    parts, rest = [], region
    each = region.area / count
    for _ in range(count - 1):
        piece, rest = cut_from_side(rest, side, each)
        parts.append(piece)
    parts.append(rest)
    return parts


def split_floor(footprint: BaseGeometry, floor: Floor,
                to_local=None) -> list[tuple[Room, str, str, BaseGeometry]]:
    """Return [(room, id, name, local_geometry)] for one floor.

    Plan rooms (real shapes, `to_local` projects them) are placed first, in
    order, each trimmed by the rooms placed before it. They are not cut to the
    footprint (OSM outlines are coarse): a room may stick out of it.
    Ratio rooms then divide what is left of the footprint.
    """
    total = footprint.area
    remaining = footprint
    placed: BaseGeometry = Polygon()
    out: list[tuple[Room, str, str, BaseGeometry]] = []
    for room in floor.rooms:
        if room.side == "plan":
            piece = _polygonal(to_local(shape(room.geometry)).difference(placed))
            remaining = _polygonal(remaining.difference(piece))
        elif room.side == "rest":
            piece, remaining = remaining, Polygon()
        else:
            piece, remaining = cut_from_side(remaining, room.side, room.ratio * total)
        placed = _polygonal(unary_union([placed, piece])) if not piece.is_empty else placed
        if room.subdivide:
            subs = subdivide(piece, room.subdivide["count"], room.subdivide.get("axis", "long"))
            for k, sub in enumerate(subs, start=1):
                out.append((room, f"{room.id}-{k}", f"{room.name} {k}", sub))
        else:
            out.append((room, room.id, room.name, piece))
    if remaining.area >= MIN_PART_AREA:
        if floor.has_plan:
            # On a drawn plan the space between rooms is corridors and common areas.
            filler = Room(id=f"L{floor.level}-common", name="共用部・通路", side="rest", ratio=None,
                          category="circulation", color=CATEGORY_COLORS["circulation"], flat=True,
                          geometry_source="remainder")
        else:
            filler = Room(id=f"L{floor.level}-unassigned", name="未割当", side="rest", ratio=None,
                          category="unassigned", color=UNASSIGNED_COLOR, confidence="estimated",
                          geometry_source="remainder")
        out.append((filler, filler.id, filler.name, remaining))
    return out


# Mapbox indoor data model (https://docs.mapbox.com/help/dive-deeper/indoor-mapping/):
# facility > structure > floor > room, linked by facility_id / structure_ids / floor_id.
# `class` must come from the indoor-v3 floorplan vocabulary
# (https://docs.mapbox.com/data/tilesets/reference/mapbox-indoor-v3/); our own `category` stays as is.
# Private rooms (offices, hotel rooms, residences) are `inaccessible`. Anything unmapped is `venue_services`.
ROOM_CLASS = {"restaurant": "restaurants", "circulation": "corridor", "service": "services",
              "unassigned": "vacant", "connector": "connector", "retail": "retail", "parking": "parking",
              "other": "inaccessible", "office": "inaccessible", "hotel": "inaccessible",
              "lobby": "corridor", "hall": "venue_services", "meeting": "venue_services"}
DEFAULT_ROOM_CLASS = "venue_services"


# Facilities drawn on a floor map get a point POI with an indoor-v3 label `icon`. A name must match exactly one kind
# (compound rooms like "休憩所・エレベーター・トイレ" and halls/goods like "エレベーターホール", "トイレ用品" are skipped).
POI_KINDS = (
    ("toilet", re.compile(r"トイレ|お手洗|WC|化粧室")),
    ("escalator", re.compile(r"エスカレーター")),
    ("elevator", re.compile(r"エレベーター")),
    ("stairs", re.compile(r"階段")),
    ("atm", re.compile(r"ATM", re.IGNORECASE)),
)
POI_SKIP = re.compile(r"ホール|用品|出口")
# Shops get a POI by category (indoor-v3 icons: shop, restaurant, cafe, bank). The name refines it: a café among the
# restaurants, a bank wherever it is listed. A facility (toilet, lift, ...) wins over a shop.
SHOP_ICONS = {"retail": "shop", "restaurant": "restaurant"}
SHOP_NAME_KINDS = (
    # `hospital` and `pharmacy` are not indoor-v3 icons (the vocabulary has none): our own, drawn as badges. The room's
    # `class` is `healthcare` (a vocabulary value) for clinics and hospitals.
    ("hospital", re.compile(r"クリニック|医院|病院|診療所|歯科|デンタル|眼科|内科|外科|皮膚科|耳鼻|小児科|産婦人科|整骨院|接骨院|"
                            r"clinic|dental", re.IGNORECASE), ("service", "office", "other", None)),
    ("pharmacy", re.compile(r"薬局|調剤|ファーマシー|ドラッグ|マツモトキヨシ|ウエルシア|ツルハ|スギ薬局|ココカラ|pharmacy", re.IGNORECASE), None),
    ("bank", re.compile(r"銀行|信用金庫|信用組合|(?<![A-Za-z])bank(?![A-Za-z])", re.IGNORECASE), None),
    ("cafe", re.compile(r"カフェ|café|cafe|珈琲|コーヒー|喫茶|スターバックス|ドトール|タリーズ|コメダ|エクセルシオール|coffee",
                        re.IGNORECASE), ("restaurant",)),
    # a shop that only sells coffee (beans, tea leaves) is retail: only a name that says so is a café
    ("cafe", re.compile(r"カフェ|café|cafe|喫茶", re.IGNORECASE), ("retail",)),
)
# A real position is needed: the shape must come from a source, not from a ratio of the floor.
POI_GEOMETRY_SOURCES = ("vector", "imagemap", "traced", "drawn", "manual")
POI_DUPLICATE_M = 10.0  # a room-name POI this close to a pictogram point of the same icon is the same facility


def poi_icon(name: str) -> str | None:
    """The label icon for a facility name, or None if it is not exactly one kind of facility."""
    if POI_SKIP.search(name):
        return None
    hits = [icon for icon, pattern in POI_KINDS if pattern.search(name)]
    if len(hits) != 1:
        return None
    if hits[0] == "toilet":
        male, female = re.search(r"男", name), re.search(r"女", name)
        if male and not female:
            return "toilet-male"
        if female and not male:
            return "toilet-female"
    return hits[0]


def shop_icon(name: str, category: str | None) -> str | None:
    """The label icon of a shop or restaurant from its category and name, or None for any other room."""
    for icon, pattern, categories in SHOP_NAME_KINDS:
        if (categories is None or category in categories) and pattern.search(name):
            return icon
    return SHOP_ICONS.get(category or "")


def poi_parts(name: str) -> list[tuple[str, str]]:
    """(label, icon) of each facility a room name stands for. A name that is one facility gives itself; a compound
    name ("エレベーター・エスカレーター・トイレ") is split at "・" outside brackets, and the parts that are not a
    facility ("休憩所", "出口", "バスルーム・トイレ用品") are dropped."""
    icon = poi_icon(name)
    if icon:
        return [(name, icon)]
    parts, depth, cur = [], 0, ""
    for ch in name:
        depth += ch in "（("
        depth -= ch in "）)"
        if ch == "・" and depth <= 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    if len(parts) < 2:
        return []
    return [(part, i) for part in parts if (i := poi_icon(part))]


def _poi_points(shape, n: int):
    """n points inside a room shape, spread along its long axis so the facilities of one room do not stack."""
    if n == 1:
        return [shape.representative_point()]
    from shapely.geometry import LineString, Point

    c = list(shape.minimum_rotated_rectangle.exterior.coords)[:4]
    i = 0 if Point(c[0]).distance(Point(c[1])) >= Point(c[1]).distance(Point(c[2])) else 1
    mid = lambda p, q: ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
    axis = LineString([mid(c[(i + 3) % 4], c[i]), mid(c[i + 1], c[(i + 2) % 4])])
    pts = [axis.interpolate((k + 1) / (n + 1), normalized=True) for k in range(n)]
    return [pt if shape.contains(pt) else shape.representative_point() for pt in pts]


def up_bearing(corners: list) -> float:
    """Compass direction (deg, 0 = north, clockwise) that the top of a floor-map image points to, from its corners
    (top-left, top-right, bottom-right, bottom-left). Shown as the map's bearing, it puts the image's top at the top
    of the screen."""
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = corners
    lat = math.radians((y0 + y1 + y2 + y3) / 4)
    east = ((x0 + x1) - (x2 + x3)) / 2 * math.cos(lat)
    north = ((y0 + y1) - (y2 + y3)) / 2
    return round(math.degrees(math.atan2(east, north)) % 360, 1)


def is_area_room(props: dict) -> bool:
    """A room with a floor area: not the exterior wall, and not a point-shaped POI (`shape_type: poi`)."""
    return props.get("type") == "room" and props.get("class") != "wall" and props.get("shape_type") != "poi"


def floor_label(name: str) -> str:
    """Short floor button label: 1F -> 1, B1F -> B1; anything else (PH, RF, ...) as is."""
    m = re.fullmatch(r"(B?\d+)F", name.strip(), flags=re.IGNORECASE)
    return m.group(1).upper() if m else name


def floor_z_index(level: int) -> int:
    """0 = ground, negative = below grade. Our levels have no 0: 1F is the ground floor."""
    return level - 1 if level > 0 else level


def indoor_ids(slug: str, level: int) -> tuple[str, str, str]:
    """(facility id, structure id, floor id), unique across buildings so several can share one source."""
    return f"facility-{slug}", f"structure-{slug}", f"floor-{slug}-{f'b{-level}' if level < 0 else level}"


def _round_coords(obj, nd=COORD_PRECISION):
    if isinstance(obj, (list, tuple)):
        if obj and isinstance(obj[0], (int, float)):
            return [round(v, nd) for v in obj]
        return [_round_coords(o, nd) for o in obj]
    return obj


def _geometry_json(geom: BaseGeometry) -> dict:
    m = mapping(geom)
    return {"type": m["type"], "coordinates": _round_coords(m["coordinates"])}


def config_hash(raw: dict) -> str:
    return hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def footprint_source(source: dict, base_dir: Path) -> str:
    """Where the footprint comes from, for the preview's label: "osm", "manual" (a GeoJSON file) or "mapbox" (a shape that older
    builds extracted from Mapbox Streets; see `footprint_warning`)."""
    src = source or {}
    if src.get("type") != "geojson":
        return "osm"
    try:
        obj = json.loads((base_dir / src["path"]).read_text(encoding="utf-8"))
        props = (obj.get("properties") if obj.get("type") == "Feature" else (obj.get("features") or [{}])[0].get("properties")) or {}
    except (OSError, ValueError, KeyError, AttributeError):
        return "manual"
    return "mapbox" if str(props.get("source", "")).startswith("mapbox") else "manual"


def footprint_warning(slug: str, fc: dict) -> str | None:
    """A message when the footprint was taken from Mapbox Streets (older builds, `mapbox-footprint` no longer exists):
    the Mapbox Product Terms (1.6 no tracing / deriving / extracting, 1.9 no caching or storing, 1.10 no redistribution)
    do not allow publishing a shape extracted from Mapbox's tiles. Replace it by the OSM outline or by an outline drawn
    from the floor plan (`indoor-map` skill, A.2) before the data is published or shared."""
    if (fc.get("metadata") or {}).get("footprint_source") != "mapbox":
        return None
    return (f"{slug}: the footprint was extracted from Mapbox Streets, which the Mapbox Product Terms do not allow to be "
            "stored or redistributed: use the OSM outline or one drawn from the floor plan before publishing or sharing this data")


def generate(cfg: BuildingConfig, footprint_wgs84: BaseGeometry) -> dict:
    frame = LocalFrame(footprint_wgs84, cfg.building.get("crs", "auto"), cfg.building.get("orientation", "auto"))
    local = _polygonal(frame.to_local(footprint_wgs84))
    if local.is_empty:
        raise ValueError("building footprint is empty after projection")
    wall_gap = cfg.defaults["wall_gap"]
    ceiling_gap = cfg.defaults["ceiling_gap"]

    facility_id, structure_id, _ = indoor_ids(cfg.slug, 0)
    name = cfg.building.get("name") or cfg.slug
    center = frame.to_wgs84(local).centroid
    footprint_json = _geometry_json(frame.to_wgs84(local))
    features = [
        {"type": "Feature", "id": facility_id, "geometry": footprint_json,
         "properties": {"type": "facility", "id": facility_id, "name": name, "z_offset": 0.0}},
        {"type": "Feature", "id": structure_id, "geometry": footprint_json,
         "properties": {"type": "structure", "id": structure_id, "name": name, "facility_id": facility_id, "z_offset": 0.0,
                        "shape_type": "building",
                        "center_lon": round(center.x, COORD_PRECISION), "center_lat": round(center.y, COORD_PRECISION)}},
    ]
    floor_ids = {f.level: indoor_ids(cfg.slug, f.level)[2] for f in cfg.floors}
    default_level = (1 if 1 in floor_ids else min((lv for lv in floor_ids if lv >= 1), default=None))
    if default_level is None:
        default_level = max(floor_ids)
    for floor in cfg.floors:
        fid = floor_ids[floor.level]
        base = round(floor.base_height, 3)
        top = round(floor.base_height + floor.height - ceiling_gap, 3)
        area = local
        if floor.outline:
            limit = frame.to_local(shape(floor.outline))
            # A drawn plan is more detailed than an outline (OSM parts are often simplified),
            # so rooms on the plan extend the floor, within the building footprint.
            plan_rooms = [frame.to_local(shape(r.geometry)) for r in floor.rooms if r.side == "plan"]
            if plan_rooms:
                limit = unary_union([limit, *plan_rooms])
            # outline_clip: false = a floor drawn from a map: its shape is kept even where it sticks out of the footprint
            area = _polygonal(limit if not floor.outline_clip else local.intersection(limit))
            if area.is_empty:
                raise ValueError(f"level {floor.level}: outline does not overlap the footprint")
        pieces = split_floor(area, floor, lambda g: _polygonal(frame.to_local(g)))
        # The slab covers only the built part of the floor (voids excluded).
        void_area = unary_union([g for room, *_, g in pieces if room.void and not g.is_empty])
        # plan rooms are not cut to the footprint, so the slab and the wall follow them where they stick out
        floor_area = _polygonal(unary_union([area, *[g for room, *_, g in pieces if room.side == "plan"]]))
        slab = _polygonal(floor_area.difference(void_area)) if not void_area.is_empty else floor_area
        if not slab.is_empty:
            slab_json = _geometry_json(frame.to_wgs84(slab))
            features.append({
                "type": "Feature", "id": fid,
                "properties": {
                    # indoor data model; every floor lists all the others as conflicting (both directions)
                    "id": fid, "type": "floor", "name": floor_label(floor.name), "description": floor.name,
                    "structure_ids": structure_id, "z_index": floor_z_index(floor.level),
                    "ground_z_offset": base,
                    "is_default": floor.level == default_level,
                    "conflicted_floor_ids": ";".join(v for k, v in floor_ids.items() if k != floor.level),
                    # ours: the extruded slab (height = its top, as on rooms) and the preview's floor filter
                    "level": floor.level, "floor_name": floor.name, "category": "floor",
                    "base_height": base, "height": round(base + SLAB_THICKNESS, 3), "z_offset": 0.0,
                    "color": "#CFCFCF", "area_m2": round(slab.area, 1),
                },
                "geometry": slab_json,
            })
            wall_id = f"wall-{fid}-exterior"
            features.append({
                "type": "Feature", "id": wall_id,
                "properties": {"type": "room", "id": wall_id, "name": "外壁", "floor_id": fid, "class": "wall",
                               "level": floor.level, "floor_name": floor.name,
                               # ground-relative: the indoor renderer shows the active floor at street level
                               "wall_height": round(top - base - SLAB_THICKNESS, 3),
                               "z_offset": 0.0, "lift": max(0.0, round(base + SLAB_THICKNESS, 3))},
                "geometry": slab_json,
            })
        # Pictograms on the floor map (toilet, lift, ...) are point POIs of their own, with no room behind them. Their
        # position is read from the drawing, a room-name POI only sits inside its room, so the pictogram wins.
        picts = [(pt, pt_local) for pt in floor.points
                 for pt_local in [frame.to_local(shape(pt["geometry"]))] if area.intersects(pt_local)]
        for room, rid, name, geom in pieces:
            if geom.is_empty or room.void:
                continue
            room_area = geom.area
            shrunk = geom.buffer(-wall_gap / 2, join_style="mitre") if wall_gap > 0 else geom
            shrunk = _polygonal(shrunk)
            if shrunk.is_empty:
                shrunk = geom
            shop = (shop_icon(name, room.category) if room.geometry_source in POI_GEOMETRY_SOURCES and not poi_parts(name)
                    else None)
            props = {
                "id": f"room-{cfg.slug}-{rid}", "room_id": rid, "name": name, "type": "room", "shape_type": "room",
                "floor_id": fid,
                "class": "healthcare" if shop == "hospital" else ROOM_CLASS.get(room.category, DEFAULT_ROOM_CLASS),
                "level": floor.level, "floor_name": floor.name,
                "category": room.category or "other",
                "base_height": round(base + SLAB_THICKNESS, 3),
                # z_offset is the data model's: relative to the floor's own level (the floor carries ground_z_offset).
                # `lift` is ours: metres above ground for the flat `fill` layer (fill-z-offset takes no negatives:
                # basements stay on the ground).
                "z_offset": 0.0,
                "lift": max(0.0, round(base + SLAB_THICKNESS, 3)),
                "height": round(base + SLAB_THICKNESS + FLAT_HEIGHT, 3) if room.flat else top,
                "color": room.color, "area_m2": round(room_area, 1),
                "ratio": round(room_area / area.area, 4),
                "source": room.source, "confidence": room.confidence or "sourced",
                "flat": room.flat,
                # Flat values, so map style expressions can use them.
                "geometry_source": room.geometry_source,
                "georef": room.georef.get("method"),
            }
            if room.georef.get("mode"):
                props["georef_mode"] = room.georef["mode"]
            if room.georef.get("rms_m") is not None:
                props["georef_rms_m"] = room.georef["rms_m"]
            features.append({
                "type": "Feature", "id": props["id"], "properties": props,
                "geometry": _geometry_json(frame.to_wgs84(shrunk)),
            })
            parts = poi_parts(name) if room.geometry_source in POI_GEOMETRY_SOURCES else []
            if shop:
                parts = [(name, shop)]
            for (label, icon), point in zip(parts, _poi_points(shrunk, len(parts)) if parts else []):
                if any(icon == pt["icon"] and q.distance(point) < POI_DUPLICATE_M for pt, q in picts):
                    continue
                poi_id = f"poi-{cfg.slug}-{rid}" + ("" if len(parts) == 1 else f"-{icon}")
                features.append({
                    "type": "Feature", "id": poi_id,
                    "properties": {
                        "id": poi_id, "type": "room", "shape_type": "poi", "name": label, "icon": icon,
                        "floor_id": fid, "class": props["class"], "room_id": rid, "level": floor.level,
                        "floor_name": floor.name, "z_offset": 0.0, "lift": props["lift"],
                        "source": room.source, "confidence": props["confidence"],
                    },
                    "geometry": _geometry_json(frame.to_wgs84(point)),
                })

        for pt, point in picts:
            poi_id = f"poi-{cfg.slug}-{pt['id']}"
            features.append({
                "type": "Feature", "id": poi_id,
                "properties": {
                    "id": poi_id, "type": "room", "shape_type": "poi", "name": pt["name"], "icon": pt["icon"],
                    # a seat is not a service: the vocabulary has no seating class, so it is a venue space
                    "floor_id": fid, "class": DEFAULT_ROOM_CLASS if pt["icon"] in ("seat", "row-label") else ROOM_CLASS["service"],
                    "room_id": pt["id"], "level": floor.level,
                    "floor_name": floor.name, "z_offset": 0.0, "lift": max(0.0, round(base + SLAB_THICKNESS, 3)),
                    "source": pt["source"], "confidence": pt["confidence"] or "sourced",
                    **pt.get("seat", {}),
                },
                "geometry": _geometry_json(frame.to_wgs84(point)),
            })

    return {
        "type": "FeatureCollection",
        "metadata": {
            "building": cfg.building.get("name"),
            "slug": cfg.slug,
            "provenance": cfg.provenance,
            "levels": [{"level": f.level, "name": f.name, "base_height": round(f.base_height, 3),
                        "height": f.height} for f in cfg.floors],
            "footprint_area_m2": round(local.area, 1),
            "footprint": _geometry_json(frame.to_wgs84(local)),
            "basemaps": [{"level": f.level, "url": f.basemap["url"], "corners": f.basemap["corners"],
                          # compass direction of the image's top (the cropped image shown later is axis-aligned,
                          # so this is taken from the fitted corners here)
                          "up_bearing_deg": up_bearing(f.basemap["corners"]),
                          "tenant_fills": f.basemap.get("tenant_fills", []),
                          "resettable": f.basemap.get("resettable", False),
                          # The preview preloads these pairs.
                          "alignment": f.basemap.get("alignment")}
                         for f in cfg.floors if f.basemap],
            "crs": frame.crs.to_string(),
            "orientation_deg": round(frame.angle, 2),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "config_hash": config_hash(cfg.raw),
        },
        "features": features,
    }


def diff(old: dict | None, new: dict) -> dict:
    """Room-level diff between two FeatureCollections (by feature id)."""
    def index(fc):
        return {f["properties"]["id"]: f for f in (fc or {}).get("features", [])}

    a, b = index(old), index(new)
    changed = []
    for fid in a.keys() & b.keys():
        pa, pb = a[fid]["properties"], b[fid]["properties"]
        fields = sorted(k for k in pa.keys() | pb.keys() if pa.get(k) != pb.get(k))
        if a[fid]["geometry"] != b[fid]["geometry"] and "area_m2" not in fields:
            fields.append("geometry")
        if fields:
            changed.append({"id": fid, "fields": fields,
                            "before": {k: pa.get(k) for k in fields if k != "geometry"},
                            "after": {k: pb.get(k) for k in fields if k != "geometry"}})
    return {
        "added": sorted(b.keys() - a.keys()),
        "removed": sorted(a.keys() - b.keys()),
        "changed": sorted(changed, key=lambda c: c["id"]),
    }


def feature_collection_from_file(path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def footprint_from_geojson(obj: dict) -> BaseGeometry:
    if obj.get("type") == "FeatureCollection":
        obj = obj["features"][0]
    if obj.get("type") == "Feature":
        obj = obj["geometry"]
    return _polygonal(shape(obj))
