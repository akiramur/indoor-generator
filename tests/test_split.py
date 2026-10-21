import pytest
from shapely.geometry import Polygon, box
from shapely import affinity

from indoor_gen import generate_geojson as gg
from indoor_gen.config import normalise
from indoor_gen.geo_utils import LocalFrame, main_axis_angle

L_SHAPE = Polygon([(0, 0), (60, 0), (60, 20), (20, 20), (20, 50), (0, 50)])


@pytest.mark.parametrize("shape", [box(0, 0, 80, 40), L_SHAPE], ids=["rect", "L"])
@pytest.mark.parametrize("side", ["west", "east", "north", "south"])
def test_cut_area_exact(shape, side):
    piece, rest = gg.cut_from_side(shape, side, shape.area * 0.37)
    assert piece.area == pytest.approx(shape.area * 0.37, rel=1e-3)
    assert piece.area + rest.area == pytest.approx(shape.area, rel=1e-6)
    assert piece.is_valid and rest.is_valid
    assert piece.intersection(rest).area < 1e-6


def test_cut_side_position():
    piece, _ = gg.cut_from_side(box(0, 0, 100, 10), "west", 400)
    assert piece.bounds[0] == pytest.approx(0) and piece.bounds[2] == pytest.approx(40, abs=1e-3)
    piece, _ = gg.cut_from_side(box(0, 0, 10, 100), "north", 300)
    assert piece.bounds[1] == pytest.approx(70, abs=1e-3)


def test_subdivide_equal():
    parts = gg.subdivide(L_SHAPE, 4)
    assert len(parts) == 4
    for p in parts:
        assert p.area == pytest.approx(L_SHAPE.area / 4, rel=1e-3)


def _cfg(rooms, **floor):
    raw = {"building": {"name": "t", "source": {"type": "latlon", "lat": 35.68, "lon": 139.76}},
           "floors": [{"level": 1, "rooms": rooms, **floor}]}
    return normalise(raw, "t")


def test_split_floor_ratios_and_unassigned():
    cfg = _cfg([{"name": "A", "side": "west", "ratio": 0.4}, {"name": "B", "side": "north", "ratio": 0.3}])
    out = gg.split_floor(L_SHAPE, cfg.floors[0])
    areas = {rid: g.area / L_SHAPE.area for _, rid, _, g in out}
    assert areas["L1-a"] == pytest.approx(0.4, abs=0.005)
    assert areas["L1-b"] == pytest.approx(0.3, abs=0.005)
    assert areas["L1-unassigned"] == pytest.approx(0.3, abs=0.005)
    geoms = [g for *_, g in out]
    for i in range(len(geoms)):
        for j in range(i + 1, len(geoms)):
            assert geoms[i].intersection(geoms[j]).area < 1e-6


def test_main_axis_folding():
    rotated = affinity.rotate(box(0, 0, 100, 20), 30, origin="centroid")
    assert main_axis_angle(rotated) == pytest.approx(30, abs=0.01)
    rotated = affinity.rotate(box(0, 0, 100, 20), 80, origin="centroid")
    assert main_axis_angle(rotated) == pytest.approx(-10, abs=0.01)


def _wgs_box():
    # ~90m x 45m building near Tokyo Station
    return box(139.7630, 35.6810, 139.7640, 35.6814)


def test_generate_end_to_end():
    raw = {
        "building": {"name": "t", "source": {"type": "latlon", "lat": 35.68, "lon": 139.76}},
        "defaults": {"floor_height": 4.0, "ceiling_gap": 0.3, "wall_gap": 0.3},
        "floors": [
            {"level": 1, "height": 5.0, "rooms": [
                {"id": "L1-lobby", "name": "ロビー", "category": "lobby", "side": "south", "ratio": 0.25},
                {"id": "L1-shops", "name": "店舗", "category": "retail", "side": "rest", "subdivide": {"count": 3}}]},
            {"levels": [2, 3], "copy_from": 1},
            {"level": -1, "rooms": [{"name": "駐車場", "category": "parking", "side": "rest"}]},
        ],
    }
    fc = gg.generate(normalise(raw, "t"), _wgs_box())
    rooms = [f["properties"] for f in fc["features"] if f["properties"]["type"] == "room" and f["properties"]["class"] != "wall"]
    ids = [r["room_id"] for r in rooms]
    assert len(ids) == len(set(ids))
    assert {"L1-lobby", "L2-lobby", "L3-shops-2", "L1-shops-3"} <= set(ids)
    by = {r["room_id"]: r for r in rooms}
    assert by["L1-lobby"]["base_height"] == pytest.approx(0.15)
    assert by["L1-lobby"]["height"] == pytest.approx(4.7)
    assert by["L2-lobby"]["base_height"] == pytest.approx(5.15)
    assert by["L3-lobby"]["height"] == pytest.approx(9.0 + 4.0 - 0.3)
    parking = next(r for r in rooms if r["level"] == -1)
    assert parking["base_height"] == pytest.approx(-4.0 + 0.15)
    assert by["L1-lobby"]["ratio"] == pytest.approx(0.25, abs=0.005)


def test_ids_stable_when_ratio_changes():
    base = {"building": {"name": "t", "source": {"type": "latlon", "lat": 35.68, "lon": 139.76}},
            "floors": [{"level": 1, "rooms": [{"name": "レストラン", "side": "north", "ratio": 0.5},
                                               {"name": "Office", "side": "rest"}]}]}
    a = gg.generate(normalise(base, "t"), _wgs_box())
    base["floors"][0]["rooms"][0]["ratio"] = 0.3
    b = gg.generate(normalise(base, "t"), _wgs_box())
    ids = lambda fc: sorted(f["properties"]["id"] for f in fc["features"])
    assert ids(a) == ids(b)
    d = gg.diff(a, b)
    assert not d["added"] and not d["removed"]
    assert {c["id"] for c in d["changed"]} == {f for f in ids(a) if f.startswith("room-")}


def test_local_frame_roundtrip():
    poly = _wgs_box()
    frame = LocalFrame(poly)
    back = frame.to_wgs84(frame.to_local(poly))
    assert back.symmetric_difference(poly).area < 1e-12
    assert frame.to_local(poly).area == pytest.approx(90 * 44.4, rel=0.05)


def test_void_rooms_are_not_emitted():
    raw = {"building": {"name": "t", "source": {"type": "latlon", "lat": 35.68, "lon": 139.76}},
           "floors": [{"level": 1, "rooms": [
               {"id": "L1-atrium", "name": "吹抜", "side": "west", "ratio": 0.3, "void": True},
               {"id": "L1-shop", "name": "Shop", "side": "rest"}]}]}
    fc = gg.generate(normalise(raw, "t"), _wgs_box())
    ids = {f["properties"].get("room_id", f["properties"]["id"]) for f in fc["features"]}
    assert "L1-atrium" not in ids and "L1-unassigned" not in ids
    shop = next(f["properties"] for f in fc["features"] if f["properties"].get("room_id") == "L1-shop")
    assert shop["ratio"] == pytest.approx(0.7, abs=0.005)


def test_slab_excludes_voids():
    raw = {"building": {"name": "t", "source": {"type": "latlon", "lat": 35.68, "lon": 139.76}},
           "floors": [{"level": 1, "rooms": [
               {"id": "L1-out", "name": "外", "side": "north", "ratio": 0.6, "void": True},
               {"id": "L1-office", "name": "Office", "side": "rest"}]}]}
    fc = gg.generate(normalise(raw, "t"), _wgs_box())
    slab = next(f["properties"] for f in fc["features"] if f["properties"]["type"] == "floor")
    assert slab["area_m2"] == pytest.approx(fc["metadata"]["footprint_area_m2"] * 0.4, rel=0.01)


def test_plan_rooms_then_ratio_rooms_then_flat_common(tmp_path):
    import json
    poly = box(139.76300, 35.68100, 139.76330, 35.68114)  # inside _wgs_box, SW corner
    plan = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"id": "L1-t01", "name": "Shop 1", "category": "retail"},
         "geometry": {"type": "Polygon", "coordinates": [list(poly.exterior.coords)]}}]}
    (tmp_path / "plan.geojson").write_text(json.dumps(plan))
    raw = {"building": {"name": "t", "source": {"type": "latlon", "lat": 35.68, "lon": 139.76}},
           "floors": [{"level": 1, "plan": "plan.geojson",
                       "rooms": [{"id": "L1-lobby", "name": "Lobby", "side": "north", "ratio": 0.3}]},
                      {"level": 2, "copy_from": 1}]}
    from indoor_gen.config import validate_raw
    assert validate_raw(raw, base_dir=tmp_path) == []
    fc = gg.generate(normalise(raw, "t", base_dir=tmp_path), _wgs_box())
    by = {f["properties"].get("room_id", f["properties"]["id"]): f["properties"] for f in fc["features"]
          if f["properties"].get("shape_type") != "poi"}
    assert {"L1-t01", "L1-lobby", "L1-common", "L2-t01", "L2-common"} <= set(by)
    assert by["L1-lobby"]["ratio"] == pytest.approx(0.3, abs=0.005)
    total = sum(p["ratio"] for p in by.values() if p.get("level") == 1 and p["type"] == "room" and p["class"] != "wall")
    assert total == pytest.approx(1.0, abs=0.01)
    assert by["L1-common"]["height"] < by["L1-t01"]["height"]


def test_rooms_record_where_their_shape_came_from(tmp_path):
    import json
    poly = box(139.76300, 35.68100, 139.76330, 35.68114)
    plan = {"type": "FeatureCollection",
            "metadata": {"geometry_source": "traced",
                         "georef": {"method": "point_pairs", "mode": "similarity", "rms_m": 4.8, "pairs": 8}},
            "features": [
                {"type": "Feature", "properties": {"id": "L1-t01", "name": "Shop 1"},
                 "geometry": {"type": "Polygon", "coordinates": [list(poly.exterior.coords)]}}]}
    (tmp_path / "plan.geojson").write_text(json.dumps(plan))
    raw = {"building": {"name": "t", "source": {"type": "latlon", "lat": 35.68, "lon": 139.76}},
           "floors": [{"level": 1, "plan": "plan.geojson",
                       "rooms": [{"id": "L1-lobby", "name": "Lobby", "side": "north", "ratio": 0.3}]},
                      {"level": 2, "rooms": [{"id": "L2-a", "name": "A", "side": "north", "ratio": 0.5}]}]}
    fc = gg.generate(normalise(raw, "t", base_dir=tmp_path), _wgs_box())
    by = {f["properties"].get("room_id", f["properties"]["id"]): f["properties"] for f in fc["features"]}
    t = by["L1-t01"]
    assert (t["geometry_source"], t["georef"], t["georef_mode"], t["georef_rms_m"]) == ("traced", "point_pairs", "similarity", 4.8)
    assert (by["L1-lobby"]["geometry_source"], by["L1-lobby"]["georef"]) == ("ratio", "footprint")
    assert by["L1-common"]["geometry_source"] == "remainder"
    assert by["L2-unassigned"]["geometry_source"] == "remainder"


@pytest.mark.parametrize("meta,expected", [
    ({"geometry_source": "imagemap", "georef": {"method": "outline_fit"}}, ("imagemap", {"method": "outline_fit"})),
    ({"traced_from": "x.png", "alignment": "similarity"}, ("traced", {"method": "point_pairs", "mode": "similarity"})),
    ({"traced_from": "x.png", "alignment": "corners"}, ("traced", {"method": "outline_fit"})),
    ({"basemap": {"url": "x.svg"}}, ("vector", {"method": "outline_fit"})),
    ({}, ("drawn", {"method": "direct"})),
], ids=["recorded", "old-trace", "old-trace-corners", "old-import", "hand-written"])
def test_plan_provenance_for_older_plan_files(meta, expected):
    from indoor_gen.config import plan_provenance
    assert plan_provenance(meta) == expected


def test_missing_plan_file_is_reported(tmp_path):
    from indoor_gen.config import validate_raw
    raw = {"building": {"name": "t", "source": {"type": "latlon", "lat": 35.68, "lon": 139.76}},
           "floors": [{"level": 1, "plan": "nope.geojson"}]}
    errors = validate_raw(raw, base_dir=tmp_path)
    assert errors and "not found" in errors[0]["message"]


def test_floor_outline_limits_rooms_and_ratios(tmp_path):
    import json
    tower = box(139.76310, 35.68110, 139.76370, 35.68130)  # 30% of _wgs_box
    (tmp_path / "tower.geojson").write_text(json.dumps(
        {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [list(tower.exterior.coords)]}}))
    raw = {"building": {"name": "t", "source": {"type": "latlon", "lat": 35.68, "lon": 139.76}},
           "floors": [{"level": 1, "rooms": [{"id": "L1-shop", "name": "Shop", "side": "rest"}]},
                      {"levels": [2, 3], "outline": "tower.geojson",
                       "rooms": [{"id": "L2-office", "name": "Office", "side": "west", "ratio": 0.75},
                                 {"id": "L2-core", "name": "Core", "side": "rest"}]}]}
    fc = gg.generate(normalise(raw, "t", base_dir=tmp_path), _wgs_box())
    by = {f["properties"].get("room_id", f["properties"]["id"]): f["properties"] for f in fc["features"]}
    tower_share = by["floor-t-2"]["area_m2"] / by["floor-t-1"]["area_m2"]
    assert tower_share == pytest.approx(0.3, abs=0.02)
    assert by["L3-office"]["ratio"] == pytest.approx(0.75, abs=0.01)   # ratio of the tower floor
    assert by["L2-office"]["area_m2"] + by["L2-core"]["area_m2"] == pytest.approx(by["floor-t-2"]["area_m2"], rel=0.02)


def test_features_follow_the_mapbox_indoor_data_model():
    raw = {"building": {"name": "T", "source": {"type": "latlon", "lat": 35.68, "lon": 139.76}},
           "floors": [{"level": -1, "name": "B1F", "rooms": [{"name": "駐車場", "category": "parking", "side": "rest"}]},
                      {"level": 1, "rooms": [{"id": "L1-shop", "name": "Shop", "category": "restaurant", "side": "rest"}]},
                      {"level": 2, "rooms": [{"id": "L2-a", "name": "A", "side": "rest"}]}]}
    fc = gg.generate(normalise(raw, "t"), _wgs_box())
    by_type = {}
    for f in fc["features"]:
        by_type.setdefault(f["properties"]["type"], []).append(f["properties"])
    fac, st = by_type["facility"][0], by_type["structure"][0]
    assert st["facility_id"] == fac["id"] and {"center_lon", "center_lat"} <= st.keys()
    floors = {p["id"]: p for p in by_type["floor"]}
    assert {p["structure_ids"] for p in floors.values()} == {st["id"]}
    assert {(p["name"], p["z_index"]) for p in floors.values()} == {("B1", -1), ("1", 0), ("2", 1)}
    assert [p["id"] for p in floors.values() if p["is_default"]] == ["floor-t-1"]
    for fid, p in floors.items():  # every other floor, in both directions
        assert set(p["conflicted_floor_ids"].split(";")) == set(floors) - {fid}
    assert st["shape_type"] == "building"
    assert all(p["ground_z_offset"] == p["base_height"] for p in floors.values())  # the floor's own height above ground
    assert floors["floor-t-1"]["ground_z_offset"] == 0 and floors["floor-t-b1"]["ground_z_offset"] < 0 < floors["floor-t-2"]["ground_z_offset"]
    rooms = by_type["room"]
    assert all(r["z_offset"] == 0 for ps in by_type.values() for r in ps)  # relative to the floor, as in the data model
    lifts = {r["floor_id"]: r["lift"] for r in rooms if r["class"] == "restaurants" or r["class"] == "parking"}
    assert lifts["floor-t-1"] == pytest.approx(0.15) and lifts["floor-t-b1"] == 0  # ours: metres above ground
    assert all(r["floor_id"] in floors for r in rooms)
    assert all(r["shape_type"] == "room" for r in rooms if r["class"] != "wall")
    assert len({p["id"] for ps in by_type.values() for p in ps}) == sum(map(len, by_type.values()))
    assert {r["class"] for r in rooms} >= {"wall", "restaurants", "parking"}
    assert sum(r["class"] == "wall" for r in rooms) == 3  # one exterior wall per floor


INDOOR_V3_CLASSES = {"check-in", "connector", "corridor", "healthcare", "inaccessible", "parking", "post_security",
                     "pre_security", "restaurants", "retail", "security", "services", "ticketing", "transportation",
                     "travel", "vacant", "venue_services"}


def test_room_class_is_indoor_v3_vocabulary():
    from indoor_gen.generate_geojson import DEFAULT_ROOM_CLASS, ROOM_CLASS
    assert set(ROOM_CLASS.values()) | {DEFAULT_ROOM_CLASS} <= INDOOR_V3_CLASSES


def test_connector_category_maps_to_connector_class():
    from indoor_gen.generate_geojson import ROOM_CLASS
    assert ROOM_CLASS["connector"] == "connector"


def test_is_area_room_excludes_walls_and_pois():
    assert gg.is_area_room({"type": "room", "class": "retail", "shape_type": "room"})
    assert not gg.is_area_room({"type": "room", "class": "wall"})
    assert not gg.is_area_room({"type": "room", "class": "services", "shape_type": "poi"})
    assert not gg.is_area_room({"type": "floor"})


@pytest.mark.parametrize("name,icon", [
    ("男性トイレ", "toilet-male"), ("女性トイレ", "toilet-female"), ("トイレ（丸の内地下北口）", "toilet"),
    ("非常用エレベーター", "elevator"), ("中央通路階段（B1F・中央通路へ）", "stairs"), ("エスカレーター", "escalator"),
    ("トイレ（丸の内地下北口・男性）", "toilet-male"), ("エレベーターホール", None), ("出口・エレベーター", None), ("バスルーム・トイレ用品", None), ("休憩所・エレベーター・トイレ", None),
    ("オフィス（西）", None),
])
def test_poi_icon(name, icon):
    assert gg.poi_icon(name) == icon


@pytest.mark.parametrize("name,category,icon", [
    ("ユニクロ", "retail", "shop"), ("和食 さくら", "restaurant", "restaurant"),
    ("スターバックス コーヒー", "restaurant", "cafe"), ("みずほ銀行", "service", "bank"),
    ("カルディコーヒーファーム", "retail", "shop"), ("E カフェ", "retail", "cafe"),
    ("SoftBank × Y!mobile", "retail", "shop"), ("貸室", "office", None),
    ("A デンタルクリニック", "service", "hospital"), ("B眼科", "service", "hospital"),
    ("C薬局", "service", "pharmacy"), ("マツモトキヨシ", "service", "pharmacy"), ("D ファーマシー", "service", "pharmacy"),
    ("歯ブラシ専門店", "retail", "shop"), ("イベント広場", "hall", None), ("案内所", "service", None), ("店", None, None),
])
def test_shop_icon(name, category, icon):
    assert gg.shop_icon(name, category) == icon


def test_poi_parts_split_compound_facility_names():
    from indoor_gen.generate_geojson import poi_parts

    assert poi_parts("多機能トイレ") == [("多機能トイレ", "toilet")]
    assert poi_parts("トイレ（丸の内地下北口・男性）") == [("トイレ（丸の内地下北口・男性）", "toilet-male")]
    assert [i for _, i in poi_parts("エレベーター・エスカレーター・トイレ")] == ["elevator", "escalator", "toilet"]
    assert [i for _, i in poi_parts("休憩所・エレベーター・トイレ")] == ["elevator", "toilet"]
    assert [i for _, i in poi_parts("出口・エレベーター")] == ["elevator"]
    assert poi_parts("バスルーム・トイレ用品") == [] and poi_parts("エレベーターホール") == []


def test_plan_points_become_pois_and_replace_a_room_poi_of_the_same_icon(tmp_path):
    import json
    poly = box(139.76300, 35.68100, 139.76330, 35.68114)
    cx, cy = poly.centroid.x, poly.centroid.y
    point = lambda i, icon, x, y: {"type": "Feature", "properties": {"id": i, "name": icon, "icon": icon},
                                   "geometry": {"type": "Point", "coordinates": [x, y]}}
    plan = {"type": "FeatureCollection", "metadata": {"geometry_source": "traced"}, "features": [
        {"type": "Feature", "properties": {"id": "L1-wc", "name": "トイレ", "category": "service"},
         "geometry": {"type": "Polygon", "coordinates": [list(poly.exterior.coords)]}},
        point("L1-pict-01", "toilet", cx, cy),                  # the room "トイレ" would also make a toilet POI: the pictogram wins
        point("L1-pict-02", "elevator", cx + 0.00001, cy),      # another facility: kept
        point("L1-pict-03", "elevator", 139.0, 35.0)]}          # outside the building: dropped
    (tmp_path / "plan.geojson").write_text(json.dumps(plan))
    raw = {"building": {"name": "t", "source": {"type": "latlon", "lat": 35.68, "lon": 139.76}},
           "floors": [{"level": 1, "plan": "plan.geojson", "rooms": []}]}
    from indoor_gen.config import validate_raw
    assert validate_raw(raw, base_dir=tmp_path) == []
    fc = gg.generate(normalise(raw, "t", base_dir=tmp_path), _wgs_box())
    pois = [f["properties"] for f in fc["features"] if f["properties"].get("shape_type") == "poi"]
    assert sorted((p["icon"], p["room_id"]) for p in pois) == [("elevator", "L1-pict-02"), ("toilet", "L1-pict-01")]


def test_plan_rooms_are_not_cut_to_the_footprint():
    cfg = _cfg([])
    floor = cfg.floors[0]
    from shapely.geometry import mapping
    def plan(rid, g):
        return gg.Room(id=rid, name=rid, side="plan", ratio=None, geometry=mapping(g))
    floor.rooms = [plan("a", box(40, 0, 80, 20)), plan("b", box(70, 5, 90, 15))]   # a sticks out of L_SHAPE (x > 60), b overlaps a
    out = {rid: g for _, rid, _, g in gg.split_floor(L_SHAPE, floor, lambda g: g)}
    assert out["a"].area == pytest.approx(800)                       # whole room kept, not cut at x = 60
    assert out["b"].area == pytest.approx(100)   # trimmed only by the earlier room: x 80-90 is left
    assert out["a"].intersection(out["b"]).area < 1e-6


def test_outline_clip_false_lets_the_floor_stick_out_of_the_footprint(tmp_path):
    import json
    beyond = box(139.76310, 35.68110, 139.76370, 35.68130)
    beyond = box(beyond.bounds[0], beyond.bounds[1], beyond.bounds[2] + 0.002, beyond.bounds[3])   # reaches past the footprint
    (tmp_path / "o.geojson").write_text(json.dumps(
        {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [list(beyond.exterior.coords)]}}))

    def floor_area(clip):
        f = {"level": 1, "outline": "o.geojson", "rooms": [{"id": "L1-a", "name": "A", "side": "rest"}]}
        if not clip:
            f["outline_clip"] = False
        raw = {"building": {"name": "t", "source": {"type": "latlon", "lat": 35.68, "lon": 139.76}}, "floors": [f]}
        fc = gg.generate(normalise(raw, "t", base_dir=tmp_path), _wgs_box())
        return next(x["properties"]["area_m2"] for x in fc["features"] if x["properties"]["type"] == "floor")

    assert floor_area(clip=False) > 1.5 * floor_area(clip=True)
