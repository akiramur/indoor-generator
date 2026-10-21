import pytest

from indoor_gen import fetch_building as fb


def _node(i, lon, lat):
    return {"type": "node", "id": i, "lon": lon, "lat": lat}


# A square split into two outer ways that only form a ring together (like relation/4856156).
API_ELEMENTS = [
    _node(1, 0.0, 0.0), _node(2, 0.001, 0.0), _node(3, 0.001, 0.001), _node(4, 0.0, 0.001),
    {"type": "way", "id": 10, "nodes": [1, 2, 3]},
    {"type": "way", "id": 11, "nodes": [3, 4, 1]},
    {"type": "relation", "id": 100, "tags": {"building": "train_station", "type": "multipolygon"},
     "members": [{"type": "way", "ref": 10, "role": "outer"}, {"type": "way", "ref": 11, "role": "outer"}]},
]


def test_relation_from_osm_api_builds_polygon():
    el = fb.osm_api_to_overpass(API_ELEMENTS, "relation", 100)
    assert el["tags"]["building"] == "train_station"
    geom = fb.element_to_geometry(el)
    assert geom is not None and geom.geom_type == "Polygon"
    assert geom.area == pytest.approx(0.001 * 0.001)


def test_way_from_osm_api():
    elements = API_ELEMENTS[:4] + [{"type": "way", "id": 12, "tags": {"building": "yes"}, "nodes": [1, 2, 3, 4, 1]}]
    geom = fb.element_to_geometry(fb.osm_api_to_overpass(elements, "way", 12))
    assert geom.area == pytest.approx(0.001 * 0.001)


def test_overpass_queries_keep_relation_members():
    # `out tags geom` drops relation members, which left relations without geometry.
    assert "body" in fb.OUT_GEOM


@pytest.mark.parametrize("category,expected", [
    ("building=yes", True), ("historic=building", True), ("building=train_station", True),
    ("railway=station", False), ("amenity=cafe", False),
])
def test_is_building_category(category, expected):
    assert fb.is_building_category(category) is expected


def test_building_by_id_falls_back_to_overpass(monkeypatch):
    def api_down(*args, **kwargs):
        raise fb.requests.ConnectionError("down")

    monkeypatch.setattr(fb.requests, "get", api_down)
    monkeypatch.setattr(fb, "overpass", lambda q: {"elements": [fb.osm_api_to_overpass(API_ELEMENTS, "relation", 100)]})
    info, geom = fb.building_by_id("relation", 100)
    assert info["osm_id"] == 100 and geom.area == pytest.approx(0.001 * 0.001)


def test_geojson_footprint_is_reread_when_the_file_changes(tmp_path):
    import json

    def square(x):
        return {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [[[x, 35.0], [x + 0.001, 35.0], [x + 0.001, 35.001], [x, 35.001], [x, 35.0]]]}}

    f, cache = tmp_path / "fp.geojson", tmp_path / "cache.geojson"
    src = {"type": "geojson", "path": "fp.geojson"}
    f.write_text(json.dumps(square(139.0)))
    first, _ = fb.resolve_footprint(src, cache, base_dir=tmp_path)
    f.write_text(json.dumps(square(139.5)))
    second, _ = fb.resolve_footprint(src, cache, base_dir=tmp_path)
    assert first.bounds[0] == 139.0 and second.bounds[0] == 139.5
