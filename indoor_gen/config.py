"""Load, validate and normalise building configs.

The config file is the single source of truth: the agent edits it, the
pipeline turns it into GeoJSON. Everything here is deterministic.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema

SCHEMA_PATH = Path(__file__).with_name("schema.json")

DEFAULTS = {
    "floor_height": 4.0,
    "ceiling_gap": 0.3,
    "wall_gap": 0.3,
    "palette": ["#4E79A7", "#F28E2B", "#E15759", "#76B7B2", "#59A14F", "#EDC948", "#B07AA1", "#FF9DA7"],
}

CATEGORY_COLORS = {
    "retail": "#F28E2B",
    "restaurant": "#E15759",
    "office": "#4E79A7",
    "lobby": "#EDC948",
    "hall": "#EDC948",
    "service": "#9C9C9C",
    "circulation": "#BAB0AC",
    "connector": "#A8A29E",
    "parking": "#7F7F7F",
    "meeting": "#76B7B2",
    "other": "#B07AA1",
}

UNASSIGNED_COLOR = "#DDDDDD"

# Where a room's shape came from, so a map can tell official shapes from ones Claude made:
#   vector    - the source has a shape per tenant (SVG paths, map JSON) and it was imported
#   imagemap  - the source page's clickable areas (<area> hotspots) were imported
#   traced    - Claude read a raster floor map and drew the outline (`main.py trace`)
#   drawn     - Claude wrote the coordinates without a drawing (plan files with no importer metadata)
#   manual    - a person drew or moved the outline in the preview (`main.py edit-rooms`)
#   ratio     - a share of the floor, cut from the footprint (no map)
#   remainder - what is left between the other rooms (common areas, unassigned)
GEOMETRY_SOURCES = ("vector", "imagemap", "traced", "drawn", "manual", "ratio", "remainder")
# How it was put on the map (`georef.method`):
#   outline_fit - the drawing's outline fitted to the building footprint (importers)
#   point_pairs - matched points (gates, entrances) on the drawing and the map; with mode / rms_m
#   direct      - coordinates written directly
#   footprint   - derived from the OSM footprint (ratio rooms, remainders)
GEOREF_METHODS = ("outline_fit", "point_pairs", "direct", "footprint")
RATIO_TOLERANCE = 1e-6


class ConfigError(Exception):
    """Raised with a list of human/agent readable problems."""

    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__("; ".join(f"{e['path']}: {e['message']}" for e in errors))


@dataclass
class Room:
    id: str
    name: str
    side: str
    ratio: float | None
    category: str | None = None
    color: str | None = None
    subdivide: dict | None = None
    source: str | None = None
    confidence: str | None = None
    void: bool = False
    flat: bool = False  # draw at floor level only (corridors, open areas)
    geometry: dict | None = None  # GeoJSON (WGS84) for rooms imported from a plan
    geometry_source: str = "ratio"  # one of GEOMETRY_SOURCES
    georef: dict = field(default_factory=lambda: {"method": "footprint"})  # {method[, mode, rms_m, pairs]}


@dataclass
class Floor:
    level: int
    name: str
    height: float
    base_height: float = 0.0
    rooms: list[Room] = field(default_factory=list)
    has_plan: bool = False
    outline: dict | None = None  # GeoJSON geometry (WGS84) limiting the floor, e.g. a tower part
    outline_clip: bool = True    # False: the outline is the floor's own shape, not cut to the footprint (a floor drawn from a map)
    basemap: dict | None = None  # {url, corners}: the source drawing behind a plan floor
    points: list[dict] = field(default_factory=list)  # point POIs of the plan: {id, name, icon, geometry, source, ...}


@dataclass
class BuildingConfig:
    raw: dict
    slug: str
    building: dict
    defaults: dict
    provenance: dict
    floors: list[Floor]


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _json_path(path) -> str:
    out = "$"
    for p in path:
        out += f"[{p}]" if isinstance(p, int) else f".{p}"
    return out


def default_floor_name(level: int) -> str:
    return f"B{-level}F" if level < 0 else f"{level}F"


def _auto_room_id(level: int, name: str) -> str:
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not ascii_slug:
        ascii_slug = "r" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
    return f"L{level}-{ascii_slug}"


def _relevel_id(room_id: str, level: int) -> str:
    """Rewrite the level part of a room id when a floor is copied."""
    m = re.match(r"^L-?\d+-(.+)$", room_id)
    return f"L{level}-{m.group(1)}" if m else f"L{level}-{room_id}"


def slug_for(path: Path, raw: dict) -> str:
    if raw.get("slug"):
        return raw["slug"]
    s = re.sub(r"[^a-z0-9]+", "-", path.stem.lower()).strip("-")
    return s or "building"


def load_raw(path: str | Path) -> dict:
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError([{"path": "$", "message": f"invalid JSON: {e}"}]) from e


def _room_color(r: dict, defaults: dict) -> str:
    color = r.get("color") or CATEGORY_COLORS.get(r.get("category", ""))
    if color:
        return color
    h = int(hashlib.sha1(r["name"].encode("utf-8")).hexdigest(), 16)
    return defaults["palette"][h % len(defaults["palette"])]


def load_outline(path: Path) -> tuple[dict | None, str | None]:
    """Read a polygon from a GeoJSON geometry / Feature / FeatureCollection."""
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"outline file not found: {path}"
    except json.JSONDecodeError as e:
        return None, f"outline file is not valid JSON: {e}"
    if obj.get("type") == "FeatureCollection":
        obj = (obj.get("features") or [{}])[0]
    if obj.get("type") == "Feature":
        obj = obj.get("geometry") or {}
    if obj.get("type") not in ("Polygon", "MultiPolygon"):
        return None, f"outline must be a Polygon/MultiPolygon, got {obj.get('type')}"
    return obj, None


def load_plan(path: Path) -> tuple[list[dict], list[str], dict]:
    """Read a plan GeoJSON. Returns (features, problems, metadata)."""
    try:
        fc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], [f"plan file not found: {path}"], {}
    except json.JSONDecodeError as e:
        return [], [f"plan file is not valid JSON: {e}"], {}
    feats, problems = [], []
    for k, f in enumerate(fc.get("features", [])):
        props = f.get("properties") or {}
        gtype = (f.get("geometry") or {}).get("type")
        if gtype not in ("Polygon", "MultiPolygon", "Point"):
            problems.append(f"plan feature {k}: geometry must be Polygon/MultiPolygon/Point, got {gtype}")
        elif gtype == "Point" and not props.get("icon"):
            problems.append(f"plan feature {k}: a Point (a pictogram's position) needs properties.icon (toilet, elevator, ...)")
        elif not props.get("id") or not props.get("name"):
            problems.append(f"plan feature {k}: properties.id and properties.name are required")
        else:
            feats.append(f)
    return feats, problems, fc.get("metadata") or {}


def plan_provenance(meta: dict) -> tuple[str, dict]:
    """(geometry_source, georef) of a plan file, from its metadata. Plans written before these
    were recorded: a trace says `traced_from`; anything else without an importer's basemap was
    written by hand."""
    if meta.get("geometry_source"):
        return meta["geometry_source"], meta.get("georef") or {"method": "outline_fit"}
    if meta.get("traced_from"):
        mode = meta.get("alignment")
        return "traced", ({"method": "point_pairs", "mode": mode} if mode and mode != "corners"
                          else {"method": "outline_fit"})
    if meta.get("basemap"):
        return "vector", {"method": "outline_fit"}
    return "drawn", {"method": "direct"}


def validate_raw(raw: dict, base_dir: Path | None = None) -> list[dict]:
    """Return a list of {path, message} problems (empty when valid)."""
    validator = jsonschema.Draft202012Validator(_schema())
    errors = [
        {"path": _json_path(e.absolute_path), "message": e.message}
        for e in sorted(validator.iter_errors(raw), key=lambda e: list(map(str, e.absolute_path)))
    ]
    if errors:
        return errors
    try:
        normalise(raw, slug="validate", base_dir=base_dir)
    except ConfigError as e:
        return e.errors
    return []


def normalise(raw: dict, slug: str, base_dir: Path | None = None) -> BuildingConfig:
    """Expand copy_from / levels, assign ids, colours and heights."""
    errors: list[dict] = []
    defaults = {**DEFAULTS, **raw.get("defaults", {})}
    source_ids = {s["id"] for s in raw.get("provenance", {}).get("sources", [])}

    # 1. Flatten floor entries into one entry per level.
    entries: dict[int, tuple[int, dict]] = {}
    for i, f in enumerate(raw["floors"]):
        levels = f["levels"] if "levels" in f else [f["level"]]
        for lvl in levels:
            if lvl in entries:
                errors.append({"path": f"$.floors[{i}]", "message": f"level {lvl} is defined more than once"})
            entries[lvl] = (i, f)

    # 2. Resolve the entry that defines each level's layout, following copy_from chains.
    def resolve_rooms(lvl: int, seen: tuple[int, ...] = ()) -> dict | None:
        i, f = entries[lvl]
        if "rooms" in f or "plan" in f:
            return f
        src = f["copy_from"]
        if src in seen or src == lvl:
            errors.append({"path": f"$.floors[{i}].copy_from", "message": f"copy_from cycle at level {lvl}"})
            return None
        if src not in entries:
            errors.append({"path": f"$.floors[{i}].copy_from", "message": f"copy_from refers to undefined level {src}"})
            return None
        return resolve_rooms(src, seen + (lvl,))

    floors: list[Floor] = []
    all_ids: dict[str, str] = {}
    for lvl in sorted(entries):
        i, f = entries[lvl]
        src = resolve_rooms(lvl)
        if src is None:
            continue
        room_dicts = src.get("rooms", [])
        copied = src is not f
        relevel = copied or "levels" in f
        if "name" in f and "levels" not in f:
            fname = f["name"]
        elif "name_template" in f:
            fname = f["name_template"].format(level=lvl, basement=-lvl)
        else:
            fname = default_floor_name(lvl)
        floor = Floor(level=lvl, name=fname, height=f.get("height", defaults["floor_height"]))

        def register(rid: str, rpath: str) -> None:
            if rid in all_ids:
                errors.append({"path": rpath, "message": f"duplicate room id '{rid}' (also at {all_ids[rid]})"})
            all_ids[rid] = rpath

        outline_path = f.get("outline") or src.get("outline")
        if outline_path:
            opath = (base_dir / outline_path) if base_dir is not None else Path(outline_path)
            floor.outline, problem = load_outline(opath)
            floor.outline_clip = f.get("outline_clip", True)
            if problem:
                errors.append({"path": f"$.floors[{i}].outline", "message": problem})

        # Rooms drawn on a plan come first; ratio rooms then divide what is left.
        if src.get("plan"):
            floor.has_plan = True
            ppath = (base_dir / src["plan"]) if base_dir is not None else Path(src["plan"])
            feats, problems, meta = load_plan(ppath)
            gsource, gref = plan_provenance(meta)
            if gsource not in GEOMETRY_SOURCES:
                problems.append(f"metadata.geometry_source must be one of {', '.join(GEOMETRY_SOURCES)}")
            floor.basemap = meta.get("basemap")
            errors.extend({"path": f"$.floors[{i}].plan", "message": m} for m in problems)
            for f_ in feats:
                pr = f_["properties"]
                rid = _relevel_id(pr["id"], lvl) if relevel else pr["id"]
                register(rid, f"{src['plan']}#{pr['id']}")
                if f_["geometry"]["type"] == "Point":
                    floor.points.append({"id": rid, "name": pr["name"], "icon": pr["icon"], "geometry": f_["geometry"],
                                         "source": pr.get("source"), "confidence": pr.get("confidence"),
                                         # a seat: section / row / number go on to the output feature
                                         "seat": {k: pr[k] for k in ("section", "row", "number", "facing_deg", "row_label", "fixed", "tier") if k in pr}})
                    continue
                floor.rooms.append(Room(
                    id=rid, name=pr["name"], side="plan", ratio=None, category=pr.get("category"),
                    color=_room_color(pr, defaults), source=pr.get("source"),
                    confidence=pr.get("confidence"), void=bool(pr.get("void", False)), flat=bool(pr.get("flat", False)),
                    geometry=f_["geometry"],
                    geometry_source=pr.get("geometry_source") or gsource, georef=pr.get("georef") or gref,
                ))

        if f.get("basemap"):
            bm = f["basemap"]
            # Resettable: an alignment can go back to the automatic placement (recorded, or the plan's own).
            floor.basemap = {"url": bm["image"], "corners": bm["corners"],
                             "resettable": "alignment" in bm and ("auto_corners" in bm or floor.basemap is not None),
                             "alignment": bm.get("alignment")}
            image = bm["image"]
            if base_dir is not None and not image.startswith(("http://", "https://")) and not (base_dir / image).exists():
                errors.append({"path": f"$.floors[{i}].basemap.image", "message": f"image not found: {image}"})

        ratio_sum = 0.0
        rest_seen = False
        used_names: dict[str, int] = {}
        for j, r in enumerate(room_dicts):
            rpath = f"$.floors[{i}].rooms[{j}]" if not copied else f"$.floors[{i}] (copied rooms[{j}])"
            if rest_seen:
                errors.append({"path": rpath, "message": "room after a side=rest room gets no area; move 'rest' to the end"})
            if r["side"] == "rest":
                rest_seen = True
            else:
                ratio_sum += r["ratio"]
            if "id" in r:
                rid = _relevel_id(r["id"], lvl) if relevel else r["id"]
            else:
                rid = _auto_room_id(lvl, r["name"])
                n = used_names.get(rid, 0) + 1
                used_names[rid] = n
                if n > 1:
                    rid = f"{rid}-{n}"
            register(rid, rpath)
            if r.get("source") and r["source"] not in source_ids:
                errors.append({"path": rpath + ".source", "message": f"unknown source id '{r['source']}'"})
            color = _room_color(r, defaults)
            floor.rooms.append(Room(
                id=rid, name=r["name"], side=r["side"], ratio=r.get("ratio"),
                category=r.get("category"), color=color, subdivide=copy.deepcopy(r.get("subdivide")),
                source=r.get("source"), confidence=r.get("confidence"), void=r.get("void", False), flat=r.get("flat", False),
            ))
        if ratio_sum > 1 + RATIO_TOLERANCE:
            errors.append({
                "path": f"$.floors[{i}]",
                "message": f"level {lvl}: room ratios sum to {ratio_sum:.3f} (> 1.0)",
            })
        floors.append(floor)

    if errors:
        raise ConfigError(errors)

    # 3. Heights: ground and above stack up from 0, basements stack down.
    z = 0.0
    for fl in [f for f in floors if f.level >= 0]:
        fl.base_height = z
        z += fl.height
    z = 0.0
    for fl in sorted([f for f in floors if f.level < 0], key=lambda f: -f.level):
        z -= fl.height
        fl.base_height = z

    return BuildingConfig(
        raw=raw,
        slug=slug,
        building=raw["building"],
        defaults=defaults,
        provenance=raw.get("provenance", {}),
        floors=floors,
    )


def load(path: str | Path) -> BuildingConfig:
    path = Path(path)
    raw = load_raw(path)
    errors = validate_raw(raw, base_dir=path.parent)
    if errors:
        raise ConfigError(errors)
    return normalise(raw, slug_for(path, raw), base_dir=path.parent)
