import json

from shapely.geometry import Point, box, shape
from shapely.affinity import translate

from indoor_gen import mapbox_building as mb, realign


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _field(no, wire, payload):
    key = _varint(no << 3 | wire)
    return key + (_varint(len(payload)) + payload if wire == 2 else _varint(payload))


def _zz(n):
    return (n << 1) ^ (n >> 31)


def _tile_with_square(x0, y0, x1, y1):
    """A vector tile whose `building` layer holds one square (tile units, 4096 extent)."""
    cmds = [(1 & 7) | (1 << 3), _zz(x0), _zz(y0), (2 & 7) | (3 << 3), _zz(x1 - x0), 0, 0, _zz(y1 - y0), _zz(x0 - x1), 0, 7 | (1 << 3)]
    feature = _field(3, 0, 3) + _field(4, 2, b"".join(_varint(c) for c in cmds))
    layer = _field(1, 2, b"building") + _field(2, 2, feature) + _field(5, 0, 4096) + _field(15, 0, 2)
    return _field(3, 2, layer)


def test_decode_building_polygon():
    polys = mb.decode_buildings(_tile_with_square(1024, 1024, 3072, 3072), 16, 58_000, 25_800)
    assert len(polys) == 1 and polys[0].is_valid
    w = polys[0].bounds[2] - polys[0].bounds[0]
    assert abs(w - 0.5 * 360 / 2 ** 16) < 1e-9       # half a tile wide


def _config(tmp_path, plan_geom, corners):
    d = tmp_path / "plans"
    d.mkdir()
    (d / "1F.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"id": "a"}, "geometry": plan_geom},
        {"type": "Feature", "properties": {"id": "p"}, "geometry": {"type": "Point", "coordinates": plan_geom["coordinates"][0][0]}}]}))
    return {"building": {"name": "t", "source": {"type": "latlon", "lat": 35.68, "lon": 139.76}},
            "floors": [{"level": 1, "plan": "plans/1F.geojson",
                        "basemap": {"image": "m.png", "corners": corners,
                                    "alignment": {"mode": "affine", "pairs": [{"px": [0, 0], "lnglat": corners[0]}]}}}]}


def test_realign_moves_plans_and_maps_together(tmp_path):
    fp = box(139.7600, 35.6800, 139.7612, 35.6809)
    off = translate(fp, 0.0002, -0.00012)                                  # the plan sits ~20 m off the footprint
    plan = json.loads(json.dumps(shape(off.__geo_interface__).__geo_interface__))
    corners = [[c[0], c[1]] for c in list(off.exterior.coords)[:4]]
    raw = _config(tmp_path, plan, corners)
    res = realign.fit(raw, tmp_path, fp)
    assert res["moved"] and res["after"] > res["before"] + 0.2
    realign.apply(raw, tmp_path, res)
    moved_plan = shape(json.loads((tmp_path / "plans" / "1F.geojson").read_text())["features"][0]["geometry"])
    assert moved_plan.intersection(fp).area / moved_plan.union(fp).area > 0.9
    c0 = raw["floors"][0]["basemap"]["corners"][0]
    assert Point(c0).distance(Point(corners[0])) > 1e-4                     # the map moved ...
    assert raw["floors"][0]["basemap"]["alignment"]["pairs"][0]["lnglat"][0] == c0[0]   # ... with its point pairs
    assert (tmp_path / ".history" / "plans").exists()                        # the plan was backed up


def test_realign_leaves_a_good_fit_alone(tmp_path):
    fp = box(139.7600, 35.6800, 139.7612, 35.6809)
    raw = _config(tmp_path, json.loads(json.dumps(fp.__geo_interface__)), [[139.76, 35.68]] * 4)
    res = realign.fit(raw, tmp_path, fp)
    assert not res["moved"]
    assert realign.apply(raw, tmp_path, res) == []


def test_footprint_source_label(tmp_path):
    from indoor_gen.generate_geojson import footprint_source

    (tmp_path / "mb.geojson").write_text(json.dumps({"type": "Feature", "properties": {"source": "mapbox-streets-v8 building"},
                                                     "geometry": None}), encoding="utf-8")
    (tmp_path / "hand.geojson").write_text(json.dumps({"type": "Feature", "properties": {"name": "x"}, "geometry": None}),
                                           encoding="utf-8")
    assert footprint_source({"type": "geojson", "path": "mb.geojson"}, tmp_path) == "mapbox"
    assert footprint_source({"type": "geojson", "path": "hand.geojson"}, tmp_path) == "manual"
    assert footprint_source({"type": "geojson", "path": "missing.geojson"}, tmp_path) == "manual"
    assert footprint_source({"type": "query", "query": "x"}, tmp_path) == "osm"


def test_realign_moves_the_floor_map_an_importer_keeps_in_the_plan(tmp_path):
    fp = box(139.7600, 35.6800, 139.7612, 35.6809)
    off = translate(fp, 0.0002, -0.00012)
    plan = json.loads(json.dumps(off.__geo_interface__))
    corners = [[c[0], c[1]] for c in list(off.exterior.coords)[:4]]
    raw = _config(tmp_path, plan, corners)
    del raw["floors"][0]["basemap"]
    path = tmp_path / "plans" / "1F.geojson"
    obj = json.loads(path.read_text())
    obj["metadata"] = {"basemap": {"url": "m.svg", "corners": corners}}
    path.write_text(json.dumps(obj))
    res = realign.fit(raw, tmp_path, fp)
    realign.apply(raw, tmp_path, res)
    moved = json.loads(path.read_text())["metadata"]["basemap"]["corners"]
    assert Point(moved[0]).distance(Point(corners[0])) > 1e-4


def test_manual_nudge_rotates_counter_clockwise_about_the_footprint(tmp_path):
    import math

    fp = box(139.7600, 35.6800, 139.7612, 35.6809)
    raw = _config(tmp_path, json.loads(json.dumps(fp.__geo_interface__)), [[139.7600, 35.6800], [139.7612, 35.6800],
                                                                          [139.7612, 35.6809], [139.7600, 35.6809]])
    before = raw["floors"][0]["basemap"]["corners"]
    realign.apply(raw, tmp_path, realign.manual(raw, tmp_path, fp, rotate_deg=1.0))
    a, b = raw["floors"][0]["basemap"]["corners"][:2]
    ang = math.degrees(math.atan2(b[1] - a[1], (b[0] - a[0]) * math.cos(math.radians(35.68))))
    assert 0.9 < ang < 1.1                                                   # the bottom edge (east) turned 1 deg counter-clockwise
    mid = [(x + y) / 2 for x, y in zip(raw["floors"][0]["basemap"]["corners"][0], raw["floors"][0]["basemap"]["corners"][2])]
    assert abs(mid[0] - 139.7606) < 1e-5 and abs(mid[1] - 35.68045) < 1e-5   # about the centre
    assert before != raw["floors"][0]["basemap"]["corners"]


def _nudge_setup(tmp_path, follows):
    fp = box(139.7600, 35.6800, 139.7612, 35.6809)
    raw = _config(tmp_path, json.loads(json.dumps(fp.__geo_interface__)), [[139.7600, 35.6800], [139.7612, 35.6800],
                                                                          [139.7612, 35.6809], [139.7600, 35.6809]])
    (tmp_path / "plans").mkdir(exist_ok=True)
    (tmp_path / "plans" / "footprint.geojson").write_text(json.dumps({"type": "Feature", "properties": {}, "geometry": fp.__geo_interface__}))
    (tmp_path / "plans" / "outline.geojson").write_text(json.dumps({"type": "Feature", "properties": {}, "geometry": box(139.7602, 35.6802, 139.7610, 35.6807).__geo_interface__}))
    raw["building"]["source"] = {"type": "geojson", "path": "plans/footprint.geojson"}
    raw["floors"][0]["outline"] = "plans/outline.geojson"
    if follows:
        raw["building"]["footprint_follows_map"] = True
    return fp, raw


def test_nudge_moves_a_footprint_drawn_from_the_maps_together_with_them(tmp_path):
    fp, raw = _nudge_setup(tmp_path, follows=True)
    realign.apply(raw, tmp_path, realign.manual(raw, tmp_path, fp, scale=1.1, east_m=10))
    new_fp = shape(json.loads((tmp_path / "plans" / "footprint.geojson").read_text())["geometry"])
    assert abs(new_fp.area / fp.area - 1.21) < 0.01                          # scaled 1.1 in both directions
    out = shape(json.loads((tmp_path / "plans" / "outline.geojson").read_text())["geometry"])
    assert out.centroid.x > box(139.7602, 35.6802, 139.7610, 35.6807).centroid.x   # the outline moved east too
    bm = raw["floors"][0]["basemap"]["corners"]
    xs, ys = [c[0] for c in bm], [c[1] for c in bm]
    assert all(abs(a - b) < 1e-6 for a, b in zip(new_fp.bounds, (min(xs), min(ys), max(xs), max(ys))))   # map and footprint stay together


def test_nudge_leaves_a_real_footprint_alone(tmp_path):
    fp, raw = _nudge_setup(tmp_path, follows=False)
    before = (tmp_path / "plans" / "footprint.geojson").read_text()
    realign.apply(raw, tmp_path, realign.manual(raw, tmp_path, fp, scale=1.1, east_m=10))
    assert (tmp_path / "plans" / "footprint.geojson").read_text() == before


def test_nudge_moves_outlines_drawn_from_the_maps_but_not_an_osm_part(tmp_path):
    fp, raw = _nudge_setup(tmp_path, follows=False)                       # a real footprint: stays
    o = tmp_path / "plans" / "outline.geojson"
    obj = json.loads(o.read_text()); obj["properties"]["follows_map"] = True; o.write_text(json.dumps(obj))
    before_fp = (tmp_path / "plans" / "footprint.geojson").read_text()
    realign.apply(raw, tmp_path, realign.manual(raw, tmp_path, fp, scale=1.1, east_m=10))
    assert (tmp_path / "plans" / "footprint.geojson").read_text() == before_fp
    moved = shape(json.loads(o.read_text())["geometry"])
    assert moved.centroid.x > box(139.7602, 35.6802, 139.7610, 35.6807).centroid.x
    plain = tmp_path / "plans" / "part.geojson"                           # an OSM building:part used as an outline: stays
    plain.write_text(json.dumps({"type": "Feature", "properties": {}, "geometry": box(139.7603, 35.6803, 139.7608, 35.6806).__geo_interface__}))
    raw["floors"][0]["outline"] = "plans/part.geojson"
    text = plain.read_text()
    realign.apply(raw, tmp_path, realign.manual(raw, tmp_path, fp, scale=1.1))
    assert plain.read_text() == text


def test_nudge_of_one_floor_leaves_the_other_floors_alone(tmp_path):
    fp = box(139.7600, 35.6800, 139.7612, 35.6809)
    corners = [[139.7600, 35.6800], [139.7612, 35.6800], [139.7612, 35.6809], [139.7600, 35.6809]]
    raw = _config(tmp_path, json.loads(json.dumps(fp.__geo_interface__)), corners)
    (tmp_path / "plans" / "2F.geojson").write_text((tmp_path / "plans" / "1F.geojson").read_text())
    raw["floors"].append({"level": 2, "plan": "plans/2F.geojson", "basemap": {"image": "m2.png", "corners": json.loads(json.dumps(corners))}})
    f1, f2 = ((tmp_path / "plans" / n).read_text() for n in ("1F.geojson", "2F.geojson"))
    realign.apply(raw, tmp_path, realign.manual(raw, tmp_path, fp, east_m=10), level=2)
    assert (tmp_path / "plans" / "1F.geojson").read_text() == f1 and raw["floors"][0]["basemap"]["corners"] == corners
    assert (tmp_path / "plans" / "2F.geojson").read_text() != f2 and raw["floors"][1]["basemap"]["corners"][0][0] > corners[0][0]
