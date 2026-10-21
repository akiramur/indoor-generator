"""Hand edits of room shapes made in the preview (`main.py edit-rooms`).

The preview sends operations on one floor's rooms: move the vertices of a room (`update`), `delete` a room, `add`
a new one. Hand-drawn shapes live in the floor's plan GeoJSON (`geometry_source: manual`), so a rebuild keeps them:
a floor without a plan gets `plans/<slug>/<floor>-manual.geojson`, and a ratio room that is edited moves from the
config's `rooms` into the plan (the other ratio rooms keep their areas but shift to fill what is left).
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from shapely.geometry import shape

from indoor_gen import config

MANUAL = {"geometry_source": "manual", "georef": {"method": "direct"}}
MIN_AREA_M2 = 0.5


class EditError(Exception):
    pass


def _floor_label(level: int) -> str:
    return f"B{-level}" if level < 0 else f"{level}F"


def _clean_geometry(geom: dict, what: str) -> dict:
    """A valid Polygon / MultiPolygon with rounded coordinates, or EditError."""
    if not isinstance(geom, dict) or geom.get("type") not in ("Polygon", "MultiPolygon"):
        raise EditError(f"{what}: geometry must be a Polygon / MultiPolygon")
    try:
        rounded = json.loads(json.dumps(geom["coordinates"]), parse_float=lambda s: round(float(s), 8))
        g = {"type": geom["type"], "coordinates": rounded}
        s = shape(g)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError) as e:
        raise EditError(f"{what}: unreadable geometry ({e})") from e
    if s.is_empty or not s.is_valid:
        raise EditError(f"{what}: the outline crosses itself or has fewer than 3 distinct vertices")
    # Rough area in m² (good enough for a sanity check at building scale).
    lat = s.centroid.y
    from math import cos, radians
    if s.area * (111_320 ** 2) * cos(radians(lat)) < MIN_AREA_M2:
        raise EditError(f"{what}: the shape is smaller than {MIN_AREA_M2} m²")
    return g


def _rename(props: dict, op: dict, where: str) -> None:
    """An `update` may also set the room's name / category (a merged room takes a new name)."""
    if "name" in op:
        name = (op["name"] or "").strip()
        if not name:
            raise EditError(f"{where}: the name must not be empty")
        props["name"] = name
    if op.get("category"):
        props["category"] = op["category"]


def _backup(path: Path, root: Path) -> None:
    if path.exists():
        d = root / ".history" / "plans"
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, d / f"{datetime.now().strftime('%Y%m%dT%H%M%S')}_{path.parent.name}_{path.name}")


def apply_edits(cfg_path: Path, raw: dict, level: int, ops: list[dict]) -> dict:
    """Apply `ops` to the floor at `level`. Mutates `raw` (the config) and writes the plan file.
    Returns {"applied": [...], "warnings": [...]}; raises EditError (nothing is written then)."""
    base = cfg_path.parent
    cfg = config.load(cfg_path)
    entry = next((f for f in raw["floors"] if f.get("level") == level), None)
    floor = next((f for f in cfg.floors if f.level == level), None)
    if entry is None or floor is None:
        raise EditError(f"no floor entry for level {level} (floors defined together with `levels` cannot be edited by hand)")
    if "rooms" not in entry and "plan" not in entry:
        raise EditError(f"level {level} copies the rooms of level {entry.get('copy_from')}; edit that floor instead")

    warnings: list[str] = []
    copies = [f["level"] for f in raw["floors"] if f.get("copy_from") == level]
    if copies:
        warnings.append(f"level(s) {copies} copy this floor's rooms and change with it")

    # Plan features (rooms) and the ratio rooms of the config, by id.
    plan_path = (base / entry["plan"]) if entry.get("plan") else None
    if plan_path is not None:
        try:
            fc = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise EditError(f"cannot read plan {plan_path}: {e}") from e
    else:
        fc = {"type": "FeatureCollection", "metadata": {"floor": _floor_label(level), **MANUAL}, "features": []}
    feats = fc["features"]
    plan_by_id = {f["properties"]["id"]: f for f in feats
                  if (f.get("geometry") or {}).get("type") != "Point" and (f.get("properties") or {}).get("id")}
    rooms = entry.get("rooms", [])
    ratio_ids = [r.id for r in floor.rooms if r.side != "plan"]
    raw_by_id = dict(zip(ratio_ids, rooms))
    taken = set(plan_by_id) | set(raw_by_id)

    applied = []
    for k, op in enumerate(ops):
        kind, rid = op.get("op"), op.get("id")
        where = f"ops[{k}] {kind} {rid or op.get('name') or ''}".strip()
        if kind == "add":
            name = (op.get("name") or "").strip()
            if not name:
                raise EditError(f"{where}: a new room needs a name")
            n = 1
            while f"L{level}-manual-{n}" in taken:
                n += 1
            new_id = f"L{level}-manual-{n}"
            taken.add(new_id)
            props = {"id": new_id, "name": name, "category": op.get("category") or "other",
                     "confidence": op.get("confidence") or "estimated", **MANUAL}
            f = {"type": "Feature", "properties": props, "geometry": _clean_geometry(op.get("geometry"), where)}
            feats.append(f)
            plan_by_id[new_id] = f
            applied.append({"op": "add", "id": new_id, "name": name})
        elif kind in ("update", "delete"):
            if rid in plan_by_id:
                f = plan_by_id[rid]
                if kind == "delete":
                    feats.remove(f)
                    del plan_by_id[rid]
                else:
                    f["geometry"] = _clean_geometry(op.get("geometry"), where)
                    f["properties"].update(MANUAL)
                    _rename(f["properties"], op, where)
            elif rid in raw_by_id:
                r = raw_by_id.pop(rid)
                if kind == "update" and r.get("subdivide"):
                    raise EditError(f"{where}: this room is subdivided into several parts; edit them in the config")
                rooms.remove(r)
                if kind == "update":
                    props = {"id": rid, "name": r["name"], "category": r.get("category") or "other", **MANUAL}
                    for key in ("color", "source", "confidence", "void", "flat"):
                        if r.get(key) not in (None, False):
                            props[key] = r[key]
                    _rename(props, op, where)
                    f = {"type": "Feature", "properties": props, "geometry": _clean_geometry(op.get("geometry"), where)}
                    feats.append(f)
                    plan_by_id[rid] = f
                    warnings.append(f"{r['name']} was a share of the floor (ratio); the other ratio rooms keep their areas "
                                    "but may shift to fill what is left")
            else:
                raise EditError(f"{where}: no such room on level {level} (generated rooms such as the common area, "
                                "and subdivided rooms, cannot be edited)")
            applied.append({"op": kind, "id": rid})
        else:
            raise EditError(f"{where}: unknown op")

    # Write only when everything passed.
    if plan_path is None:
        rel = f"plans/{cfg.slug}/{_floor_label(level)}-manual.geojson"
        plan_path = base / rel
        entry["plan"] = rel
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    _backup(plan_path, base)
    plan_path.write_text(json.dumps(fc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if rooms:
        entry["rooms"] = rooms
    else:
        entry.pop("rooms", None)
    return {"applied": applied, "warnings": warnings, "plan": str(plan_path)}
