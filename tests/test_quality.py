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


def test_listing_check_compares_the_tenants_a_source_lists_with_the_shops_built(tmp_path):
    import json

    fc = {"metadata": {"building": "T"}, "features": [
        _room("a", 1, 60, category="retail"), _room("b", 1, 30, category="restaurant"),
        _room("stairs", 1, 5, category="connector"),                       # not a tenant
        _room("c", 2, 40, category="retail"), _room("d", 2, 40, category="service"), _room("e", 2, 40, category="retail"),
        _room("f", 3, 40, category="retail"), _room("g", 3, 40, confidence="estimated", geometry_source="ratio",
                                                    category="retail")]}      # a ratio guess is not a shop shape
    rows = {r["floor"]: r for r in quality.listing_check(fc, {1: {"listed": 4, "excluded": 2},
                                                              2: {"listed": 2, "excluded": 0},
                                                              3: {"listed": 1, "excluded": 0},
                                                              9: {"listed": 5, "excluded": 0}})}   # no such floor: skipped
    assert (rows["1F"]["built"], rows["1F"]["diff"], rows["1F"]["verdict"]) == (2, 0, "matches")   # 4 listed - 2 left out
    assert rows["2F"]["diff"] == 1 and "more built" in rows["2F"]["verdict"]
    assert rows["3F"]["diff"] == 0 and set(rows) == {"1F", "2F", "3F"}
    short = quality.listing_check(fc, {2: {"listed": 5, "excluded": 0}})[0]
    assert short["diff"] == -2 and "fewer built" in short["verdict"]
    assert "no floor records" in quality.format_listing([])

    plan = {"type": "FeatureCollection", "metadata": {"tenants_listed": 13, "tenants_excluded": 1}, "features": []}
    (tmp_path / "1F.geojson").write_text(json.dumps(plan))
    (tmp_path / "2F.geojson").write_text(json.dumps({"type": "FeatureCollection", "metadata": {}, "features": []}))
    cfg = tmp_path / "t.json"
    cfg.write_text(json.dumps({"floors": [{"level": 1, "plan": "1F.geojson"}, {"level": 2, "plan": "2F.geojson"},
                                          {"level": 3, "rooms": []}]}))
    assert quality.plan_listings(cfg) == {1: {"listed": 13, "excluded": 1}}
    assert quality.plan_listings(tmp_path / "missing.json") == {}


def test_footprint_check_reports_how_much_traced_area_lies_inside_the_footprint():
    lon, lat, d = 139.76, 35.68, 0.0001

    def box(x0, x1, y0=0.0, y1=4.0):
        return {"type": "Polygon", "coordinates": [[[lon + x0 * d, lat + y0 * d], [lon + x1 * d, lat + y0 * d],
                                                    [lon + x1 * d, lat + y1 * d], [lon + x0 * d, lat + y1 * d],
                                                    [lon + x0 * d, lat + y0 * d]]]}

    def feat(rid, level, geom, **extra):
        f = _room(rid, level, 100, **extra)
        f["geometry"] = geom
        return f

    fc = {"metadata": {"footprint": box(0, 10), "crs": "auto"}, "features": [
        feat("a", 1, box(1, 4)), feat("b", 1, box(5, 9)),                       # both inside: 1F fits
        feat("c", 2, box(0, 9)), feat("d", 2, box(8, 11)),                      # d: a third outside; 2 sticks out a little (10 of 12 = 92%)
        feat("e", 3, box(1, 3)), feat("f", 3, box(11, 17)),                     # f: wholly outside; 3 sticks out a lot
        feat("g", 4, box(11, 17), confidence="estimated", geometry_source="ratio")]}   # not a source shape: floor skipped
    rows = {r["floor"]: r for r in quality.footprint_check(fc)}
    assert set(rows) == {"1F", "2F", "3F"}
    assert rows["1F"]["inside_pct"] == 100.0 and rows["1F"]["verdict"] == "fits" and rows["1F"]["rooms_mostly_outside"] == 0
    assert 85 <= rows["2F"]["inside_pct"] < 95 and rows["2F"]["verdict"] == "a little sticks out"
    assert rows["3F"]["inside_pct"] < 85 and rows["3F"]["rooms_mostly_outside"] == 1 and "a lot" in rows["3F"]["verdict"]
    assert "no floor" in quality.format_footprint([])
