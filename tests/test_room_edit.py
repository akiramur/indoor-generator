import json

import pytest

from indoor_gen import config, room_edit

SQ = {"type": "Polygon", "coordinates": [[[139.76, 35.68], [139.7601, 35.68], [139.7601, 35.6801], [139.76, 35.6801], [139.76, 35.68]]]}
BOWTIE = {"type": "Polygon", "coordinates": [[[139.76, 35.68], [139.7601, 35.6801], [139.7601, 35.68], [139.76, 35.6801], [139.76, 35.68]]]}


def _setup(tmp_path, plan=False):
    raw = {
        "building": {"name": "t", "source": {"type": "latlon", "lat": 35.68, "lon": 139.76}},
        "floors": [
            {"level": 1, "rooms": [{"id": "L1-a", "name": "A", "side": "west", "ratio": 0.5, "category": "retail", "source": None},
                                   {"id": "L1-b", "name": "B", "side": "rest"}]},
            {"level": 2, "copy_from": 1},
        ],
    }
    raw["floors"][0]["rooms"][0].pop("source")
    if plan:
        raw["floors"][0]["plan"] = "plans/t/1F.geojson"
        (tmp_path / "plans" / "t").mkdir(parents=True)
        (tmp_path / "plans/t/1F.geojson").write_text(json.dumps({"type": "FeatureCollection", "metadata": {"geometry_source": "traced"}, "features": [
            {"type": "Feature", "properties": {"id": "P1", "name": "Shop"}, "geometry": SQ}]}))
    path = tmp_path / "t.json"
    path.write_text(json.dumps(raw))
    return path, raw


def test_update_ratio_room_moves_it_into_a_manual_plan(tmp_path):
    path, raw = _setup(tmp_path)
    res = room_edit.apply_edits(path, raw, 1, [{"op": "update", "id": "L1-a", "geometry": SQ}])
    assert raw["floors"][0]["plan"] == "plans/t/1F-manual.geojson"
    assert [r["id"] for r in raw["floors"][0]["rooms"]] == ["L1-b"]
    fc = json.loads((tmp_path / "plans/t/1F-manual.geojson").read_text())
    p = fc["features"][0]["properties"]
    assert (p["id"], p["name"], p["category"], p["geometry_source"]) == ("L1-a", "A", "retail", "manual")
    assert res["warnings"]
    path.write_text(json.dumps(raw))
    assert config.load(path).floors[0].rooms[0].geometry_source == "manual"


def test_update_delete_add_on_a_plan(tmp_path):
    path, raw = _setup(tmp_path, plan=True)
    room_edit.apply_edits(path, raw, 1, [
        {"op": "update", "id": "P1", "geometry": SQ},
        {"op": "add", "name": "New", "category": "retail", "geometry": SQ},
        {"op": "add", "name": "New2", "geometry": SQ},
        {"op": "delete", "id": "L1-b"},
    ])
    fc = json.loads((tmp_path / "plans/t/1F.geojson").read_text())
    ids = [f["properties"]["id"] for f in fc["features"]]
    assert ids == ["P1", "L1-manual-1", "L1-manual-2"]
    assert fc["features"][0]["properties"]["geometry_source"] == "manual"
    assert fc["features"][1]["properties"]["confidence"] == "estimated"
    assert [r["id"] for r in raw["floors"][0]["rooms"]] == ["L1-a"]
    assert list((tmp_path / ".history" / "plans").glob("*1F.geojson"))  # the old plan was backed up


@pytest.mark.parametrize("op, needle", [
    ({"op": "update", "id": "L1-a", "geometry": BOWTIE}, "crosses itself"),
    ({"op": "update", "id": "L1-a", "geometry": {"type": "Point", "coordinates": [0, 0]}}, "Polygon"),
    ({"op": "delete", "id": "L1-common"}, "no such room"),
    ({"op": "add", "name": " ", "geometry": SQ}, "needs a name"),
    ({"op": "nope"}, "unknown op"),
])
def test_bad_ops_write_nothing(tmp_path, op, needle):
    path, raw = _setup(tmp_path)
    before = json.dumps(raw)
    with pytest.raises(room_edit.EditError, match=needle):
        room_edit.apply_edits(path, raw, 1, [op])
    assert not (tmp_path / "plans").exists()
    assert json.dumps(raw) == before or "plan" not in raw["floors"][0]


def test_copied_floor_cannot_be_edited(tmp_path):
    path, raw = _setup(tmp_path)
    with pytest.raises(room_edit.EditError, match="copies"):
        room_edit.apply_edits(path, raw, 2, [{"op": "delete", "id": "L2-a"}])


def test_update_can_rename_and_merge_by_delete(tmp_path):
    path, raw = _setup(tmp_path, plan=True)
    room_edit.apply_edits(path, raw, 1, [
        {"op": "update", "id": "L1-a", "geometry": SQ, "name": "A+B", "category": "office"},   # ratio room, promoted
        {"op": "delete", "id": "P1"},
    ])
    fc = json.loads((tmp_path / "plans/t/1F.geojson").read_text())
    props = {f["properties"]["id"]: f["properties"] for f in fc["features"]}
    assert "P1" not in props
    assert (props["L1-a"]["name"], props["L1-a"]["category"]) == ("A+B", "office")
    path.write_text(json.dumps(raw))
    with pytest.raises(room_edit.EditError, match="must not be empty"):
        room_edit.apply_edits(path, raw, 1, [{"op": "update", "id": "L1-a", "geometry": SQ, "name": " "}])
