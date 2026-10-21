"""Round trip of one floor's rooms through an external GeoJSON editor (geojson.io).

`export_floor` writes the floor's rooms from the build output (`id` = the config's room id, `name`, `category`);
the user edits the file elsewhere; `diff_to_ops` compares the edited file with the current rooms and returns the
operations `room_edit.apply_edits` understands: moved vertices / renamed rooms → `update`, rooms drawn without an
`id` → `add`, rooms removed from the file → `delete`.
"""

from __future__ import annotations

import json
from pathlib import Path

from indoor_gen.room_edit import EditError

ROUND = 7  # decimals compared (~1 cm); geojson.io keeps what it was given


def current_rooms(indoor_path: Path, level: int) -> dict[str, dict]:
    """The floor's room features of a build, by config room id."""
    data = json.loads(indoor_path.read_text(encoding="utf-8"))
    out = {}
    for f in data["features"]:
        p = f["properties"]
        if (p.get("shape_type") == "room" and p.get("class") != "wall" and p.get("level") == level
                and p.get("room_id") and f["geometry"]["type"] in ("Polygon", "MultiPolygon")):
            out[p["room_id"]] = f
    return out


def export_floor(indoor_path: Path, slug: str, level: int) -> dict:
    rooms = current_rooms(indoor_path, level)
    if not rooms:
        raise EditError(f"level {level} has no rooms in {indoor_path}")
    feats = [{"type": "Feature", "geometry": f["geometry"],
              "properties": {"id": rid, "name": f["properties"].get("name"), "category": f["properties"].get("category")}}
             for rid, f in rooms.items()]
    return {"type": "FeatureCollection", "metadata": {"slug": slug, "level": level, "source": "indoor-generator"},
            "features": feats}


def _key(geom: dict):
    return json.dumps(json.loads(json.dumps(geom["coordinates"]), parse_float=lambda s: round(float(s), ROUND)))


def diff_to_ops(indoor_path: Path, level: int, edited: dict) -> list[dict]:
    if not isinstance(edited, dict) or edited.get("type") != "FeatureCollection":
        raise EditError("the file must be a GeoJSON FeatureCollection")
    cur = current_rooms(indoor_path, level)
    seen: set[str] = set()
    ops: list[dict] = []
    for k, f in enumerate(edited.get("features") or []):
        geom = (f or {}).get("geometry") or {}
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue  # points / lines drawn in the editor are ignored
        props = f.get("properties") or {}
        rid = props.get("id")
        if rid is None:
            name = (props.get("name") or "").strip()
            if not name:
                raise EditError(f"features[{k}]: a new room needs a `name` property")
            ops.append({"op": "add", "name": name, "category": props.get("category") or "other", "geometry": geom})
            continue
        if rid not in cur:
            raise EditError(f"features[{k}]: id {rid!r} is not a room of level {level} (export the floor again, or clear `id` to add a new room)")
        if rid in seen:
            raise EditError(f"features[{k}]: id {rid!r} appears twice")
        seen.add(rid)
        before = cur[rid]
        changed = _key(geom) != _key(before["geometry"])
        name, cat = props.get("name"), props.get("category")
        renamed = (name or "") != (before["properties"].get("name") or "") or (cat or "") != (before["properties"].get("category") or "")
        if changed or renamed:
            op = {"op": "update", "id": rid, "geometry": geom}
            if renamed:
                op.update({"name": name, "category": cat})
            ops.append(op)
    ops += [{"op": "delete", "id": rid} for rid in cur if rid not in seen]
    return ops
