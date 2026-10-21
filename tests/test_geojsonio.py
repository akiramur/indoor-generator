import json

import pytest

from indoor_gen import geojsonio
from indoor_gen.room_edit import EditError

SQ = lambda x: {"type": "Polygon", "coordinates": [[[x, 35.0], [x + 0.001, 35.0], [x + 0.001, 35.001], [x, 35.001], [x, 35.0]]]}


def _built(tmp_path):
    feats = [{"type": "Feature", "geometry": SQ(139.0 + i * 0.01),
              "properties": {"shape_type": "room", "class": "retail", "level": 1, "room_id": rid, "name": rid, "category": "shop"}}
             for i, rid in enumerate(["a", "b"])]
    feats.append({"type": "Feature", "geometry": SQ(139.0), "properties": {"shape_type": "room", "class": "wall", "level": 1, "room_id": "w"}})
    p = tmp_path / "indoor.geojson"
    p.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    return p


def test_round_trip_and_diff(tmp_path):
    p = _built(tmp_path)
    fc = geojsonio.export_floor(p, "x", 1)
    assert [f["properties"]["id"] for f in fc["features"]] == ["a", "b"]
    assert geojsonio.diff_to_ops(p, 1, fc) == []
    fc["features"][0]["geometry"] = SQ(139.5)
    fc["features"][1]["properties"]["name"] = "B2"
    fc["features"].append({"type": "Feature", "geometry": SQ(140.0), "properties": {"name": "new"}})
    ops = geojsonio.diff_to_ops(p, 1, fc)
    assert [(o["op"], o.get("id"), o.get("name")) for o in ops] == [("update", "a", None), ("update", "b", "B2"), ("add", None, "new")]
    fc["features"] = fc["features"][:1]
    assert [(o["op"], o["id"]) for o in geojsonio.diff_to_ops(p, 1, fc)] == [("update", "a"), ("delete", "b")]


def test_errors(tmp_path):
    p = _built(tmp_path)
    with pytest.raises(EditError):
        geojsonio.diff_to_ops(p, 1, {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": SQ(1), "properties": {"id": "zzz"}}]})
    with pytest.raises(EditError):
        geojsonio.diff_to_ops(p, 1, {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": SQ(1), "properties": {}}]})
