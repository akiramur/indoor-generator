import pytest

from indoor_gen import quality


def _room(rid, level, area, confidence="sourced", geometry_source="traced", **extra):
    props = {"type": "room", "shape_type": "room", "class": "retail", "id": f"room-t-{rid}", "room_id": rid,
             "name": rid, "level": level, "floor_name": f"{level}F", "area_m2": area, "confidence": confidence,
             "geometry_source": geometry_source, **extra}
    return {"type": "Feature", "properties": props, "geometry": None}


def test_grade_of():
    assert quality.grade_of({"confidence": "estimated", "geometry_source": "ratio"}) == "estimated"
    assert quality.grade_of({"confidence": "sourced", "geometry_source": "ratio"}) == "listed"
    assert quality.grade_of({"confidence": "sourced", "geometry_source": "remainder"}) == "derived"
    assert quality.grade_of({"confidence": "sourced", "geometry_source": "traced"}) == "traced"
    assert quality.grade_of({"confidence": "sourced", "geometry_source": "vector"}) == "traced"


def test_report_shares_and_weak_rooms():
    fc = {"metadata": {"building": "T"}, "features": [
        _room("a", 1, 60), _room("b", 1, 30, confidence="estimated", geometry_source="ratio"),
        _room("c", 1, 10, geometry_source="remainder"),
        _room("d", 2, 50, geometry_source="ratio"),
        # not rooms with an area: the exterior wall and a POI
        {"type": "Feature", "properties": {"type": "room", "class": "wall", "level": 1, "id": "w"}, "geometry": None},
        _room("p", 1, 0, shape_type="poi"),
    ]}
    rep = quality.report(fc)
    assert rep["total"]["traced"]["area_pct"] == pytest.approx(40.0)
    assert [f["level"] for f in rep["floors"]] == [1, 2]
    assert rep["floors"][0]["grades"]["estimated"]["area_pct"] == pytest.approx(30.0)
    assert [w["room_id"] for w in rep["weak_rooms"]] == ["d", "b"]  # largest first
    assert "weakest rooms" in quality.format_report(rep)


def test_publish_warnings_only_above_threshold():
    solid = {"metadata": {}, "features": [_room("a", 1, 90), _room("b", 1, 10, confidence="estimated")]}
    weak = {"metadata": {}, "features": [_room("a", 1, 40), _room("b", 1, 30, confidence="estimated"),
                                         _room("c", 1, 30, geometry_source="ratio")]}
    assert quality.weak_pct(quality.report(weak)) == pytest.approx(60.0)
    warnings = quality.publish_warnings([("solid", solid), ("weak", weak)])
    assert [w["building"] for w in warnings] == ["weak"]
    assert quality.publish_warnings([("weak", weak)], threshold=60) == []  # not above


def test_change_reports_only_moved_grades(tmp_path):
    fc = {"metadata": {}, "features": [_room("a", 1, 50), _room("b", 1, 50, confidence="estimated")]}
    rep = quality.report(fc)
    assert quality.change(None, rep) is None
    assert quality.change({g: rep["total"][g]["area_pct"] for g in quality.GRADES}, rep) is None
    path = tmp_path / "quality.json"
    path.write_text(__import__("json").dumps(rep), encoding="utf-8")
    before = quality.shares_from_file(path)
    fc["features"][1] = _room("b", 1, 50)  # now sourced
    assert quality.change(before, quality.report(fc)) == {"traced": [50.0, 100.0], "estimated": [50.0, 0.0]}
    assert quality.shares_from_file(tmp_path / "missing.json") is None
