import json

import pytest

from indoor_gen import config, history

BASE = {
    "building": {"name": "t", "source": {"type": "latlon", "lat": 35.68, "lon": 139.76}},
    "provenance": {"sources": [{"id": "s1", "url": "https://example.com"}]},
    "floors": [
        {"level": 1, "rooms": [{"id": "L1-a", "name": "A", "side": "west", "ratio": 0.5, "source": "s1"},
                               {"id": "L1-b", "name": "B", "side": "rest"}]},
        {"levels": [2, 3], "copy_from": 1, "name_template": "{level}階"},
    ],
}


def test_valid_and_expanded():
    assert config.validate_raw(BASE) == []
    cfg = config.normalise(BASE, "t")
    assert [f.level for f in cfg.floors] == [1, 2, 3]
    assert cfg.floors[2].name == "3階"
    assert [r.id for r in cfg.floors[1].rooms] == ["L2-a", "L2-b"]


@pytest.mark.parametrize("mutate, needle", [
    (lambda c: c["floors"][0]["rooms"][0].update(ratio=1.2), "maximum"),
    (lambda c: c["floors"][0]["rooms"].insert(0, {"name": "X", "side": "east", "ratio": 0.6}), "sum to"),
    (lambda c: c["floors"][0]["rooms"][1].update(ratio=0.2), ""),
    (lambda c: c["floors"][1].update(copy_from=9), "undefined level 9"),
    (lambda c: c["floors"][0]["rooms"][0].update(source="nope"), "unknown source"),
    (lambda c: c["floors"][0]["rooms"].append({"name": "late", "side": "east", "ratio": 0.1}), "after a side=rest"),
    (lambda c: c["floors"].append({"level": 2, "rooms": []}), "more than once"),
    (lambda c: c["floors"][0]["rooms"][1].update(id="L1-a"), "duplicate room id"),
])
def test_errors(mutate, needle):
    raw = json.loads(json.dumps(BASE))
    mutate(raw)
    errors = config.validate_raw(raw)
    assert errors
    assert any(needle in e["message"] for e in errors), errors


def test_history_undo(tmp_path):
    p = tmp_path / "b.json"
    p.write_text(json.dumps({"v": 1}))
    history.record(p, "b")
    assert history.record(p, "b") is None  # unchanged -> no new snapshot
    p.write_text(json.dumps({"v": 2}))
    history.record(p, "b")
    history.undo(p, "b")
    assert json.loads(p.read_text()) == {"v": 1}
    # edited but never built -> undo restores last built
    p.write_text(json.dumps({"v": 3}))
    history.undo(p, "b")
    assert json.loads(p.read_text()) == {"v": 1}
    with pytest.raises(RuntimeError):
        history.undo(p, "b")


def test_floor_basemap(tmp_path):
    raw = json.loads(json.dumps(BASE))
    corners = [[139.76, 35.681], [139.761, 35.681], [139.761, 35.68], [139.76, 35.68]]
    raw["floors"][0]["basemap"] = {"image": "maps/1F.png", "corners": corners}
    errors = config.validate_raw(raw, base_dir=tmp_path)
    assert any("image not found" in e["message"] for e in errors), errors
    (tmp_path / "maps").mkdir()
    (tmp_path / "maps" / "1F.png").write_bytes(b"")
    assert config.validate_raw(raw, base_dir=tmp_path) == []
    floor = config.normalise(raw, "t", base_dir=tmp_path).floors[0]
    assert floor.basemap == {"url": "maps/1F.png", "corners": corners, "resettable": False, "alignment": None}
    raw["floors"][0]["basemap"].update(alignment={"mode": "similarity"}, auto_corners=corners)
    assert config.normalise(raw, "t", base_dir=tmp_path).floors[0].basemap["resettable"] is True
    raw["floors"][0]["basemap"]["corners"] = corners[:3]
    assert config.validate_raw(raw, base_dir=tmp_path)
