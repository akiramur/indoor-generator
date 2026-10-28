"""The generated GeoJSON against Mapbox's indoor data model.

Vocabularies are copied from the tileset reference (https://docs.mapbox.com/data/tilesets/reference/mapbox-indoor-v3/,
checked 2026-10-09); required properties come from the guide (https://docs.mapbox.com/help/dive-deeper/indoor-mapping/).
Update the sets here when the reference changes.
"""
import json

from shapely.geometry import box

from indoor_gen import generate_geojson as gg
from indoor_gen.config import normalise

FLOORPLAN_CLASSES = {"check-in", "connector", "corridor", "healthcare", "inaccessible", "parking", "post_security",
                     "pre_security", "restaurants", "retail", "security", "services", "ticketing", "transportation",
                     "travel", "vacant", "venue_services"}
FLOORPLAN_SHAPE_TYPES = {"amenity", "area", "carousel", "occupant", "poi", "room"}
LABEL_ICONS = {"airport-check-in", "atm", "bank", "bus", "cafe", "charging-station", "elevator", "escalator",
               "gate-label", "information", "lounge", "rail-metro", "ramp", "restaurant", "ride-share", "security",
               "shop", "stairs", "suitcase", "taxi", "toilet", "toilet-female", "toilet-male"}
WALL_CLASS = "wall"  # the guide's exterior wall: type room + class wall (not in the tileset's floorplan vocabulary)
REQUIRED = {"facility": ("id", "name"),
            "structure": ("id", "name", "facility_id", "center_lon", "center_lat"),
            "floor": ("id", "name", "structure_ids", "z_index"),
            "room": ("id", "name", "floor_id", "class")}


def _build(tmp_path):
    poly = box(139.76300, 35.68100, 139.76330, 35.68114)
    plan = {"type": "FeatureCollection", "metadata": {"geometry_source": "traced"},
            "features": [
                {"type": "Feature", "properties": {"id": "L1-wc", "name": "男子トイレ", "category": "service"},
                 "geometry": {"type": "Polygon", "coordinates": [list(poly.exterior.coords)]}}]}
    (tmp_path / "plan.geojson").write_text(json.dumps(plan))
    categories = ["restaurant", "circulation", "service", "retail", "parking", "office", "hotel", "lobby", "hall",
                  "meeting", "other"]
    rooms = [{"id": f"L2-{c}", "name": c, "category": c, "side": "rest" if i == len(categories) - 1 else "north",
              "ratio": None if i == len(categories) - 1 else 0.05} for i, c in enumerate(categories)]
    raw = {"building": {"name": "t", "source": {"type": "latlon", "lat": 35.68, "lon": 139.76}},
           "floors": [{"level": 1, "plan": "plan.geojson", "rooms": []},
                      {"level": 2, "rooms": rooms},
                      {"level": -1, "rooms": [{"name": "駐車場", "category": "parking", "side": "rest"}]}]}
    return gg.generate(normalise(raw, "t", base_dir=tmp_path), box(139.7630, 35.6810, 139.7640, 35.6814))


def test_output_follows_the_indoor_data_model(tmp_path):
    feats = [f["properties"] for f in _build(tmp_path)["features"]]
    by_type = {t: [p for p in feats if p["type"] == t] for t in REQUIRED}
    assert set(p["type"] for p in feats) == set(REQUIRED)
    assert len(by_type["facility"]) == len(by_type["structure"]) == 1
    for t, keys in REQUIRED.items():
        for p in by_type[t]:
            missing = [k for k in keys if p.get(k) in (None, "")]
            assert not missing, f"{t} {p['id']} lacks {missing}"
    ids = [p["id"] for p in feats]
    assert len(ids) == len(set(ids))

    floors = {p["id"]: p for p in by_type["floor"]}
    assert by_type["structure"][0]["facility_id"] == by_type["facility"][0]["id"]
    assert by_type["structure"][0]["shape_type"] == "building"
    assert sum(p["is_default"] for p in floors.values()) == 1
    assert len({p["z_index"] for p in floors.values()}) == len(floors)
    for p in floors.values():
        assert p["structure_ids"] == by_type["structure"][0]["id"]
        others = set(p["conflicted_floor_ids"].split(";")) - {""}
        assert others == set(floors) - {p["id"]}
    assert all(p["floor_id"] in floors for p in by_type["room"])


def test_room_classes_and_pois_use_the_reference_vocabulary(tmp_path):
    rooms = [f["properties"] for f in _build(tmp_path)["features"] if f["properties"]["type"] == "room"]
    classes = {p["class"] for p in rooms} - {WALL_CLASS}
    assert classes <= FLOORPLAN_CLASSES, classes - FLOORPLAN_CLASSES
    # every category the generator maps lands in the vocabulary
    assert set(gg.ROOM_CLASS.values()) | {gg.DEFAULT_ROOM_CLASS} <= FLOORPLAN_CLASSES
    shapes = {p["shape_type"] for p in rooms if p.get("shape_type")}
    assert shapes <= FLOORPLAN_SHAPE_TYPES
    pois = [p for p in rooms if p.get("shape_type") == "poi"]
    assert pois and {p["icon"] for p in pois} <= LABEL_ICONS
    assert all(p["class"] in FLOORPLAN_CLASSES for p in pois)


def test_up_bearing_is_the_compass_direction_of_the_images_top():
    from indoor_gen.generate_geojson import up_bearing

    lon, lat, d = 139.76, 35.68, 0.001
    north_up = [[lon, lat + d], [lon + d, lat + d], [lon + d, lat], [lon, lat]]
    assert up_bearing(north_up) == 0.0
    east_up = [[lon + d, lat + d], [lon + d, lat], [lon, lat], [lon, lat + d]]   # image turned so its top faces east
    assert up_bearing(east_up) == 90.0
    south_up = [north_up[2], north_up[3], north_up[0], north_up[1]]
    assert up_bearing(south_up) == 180.0


def test_pictograms_stay_within_a_margin_of_the_footprint_and_are_dropped_beyond_it(tmp_path):
    """A pictogram read off a floor map keeps its place up to POI_FOOTPRINT_MARGIN_M outside the footprint (the OSM
    outline can be a few metres short); farther out it is not part of the building."""
    footprint = box(139.7630, 35.6810, 139.7640, 35.6814)
    lat = 35.6812

    def at_east(metres):  # a point `metres` east of the footprint's east edge
        return 139.7640 + metres / (111_320 * 0.8135)

    def poi(name, metres):
        return {"type": "Feature", "properties": {"id": name, "name": name, "icon": "toilet", "confidence": "sourced"},
                "geometry": {"type": "Point", "coordinates": [at_east(metres), lat]}}

    plan = {"type": "FeatureCollection", "metadata": {"geometry_source": "traced"},
            "features": [poi("inside", -10), poi("close", 3), poi("far", 12)]}
    (tmp_path / "plan.geojson").write_text(json.dumps(plan))
    raw = {"building": {"name": "t", "source": {"type": "latlon", "lat": 35.6812, "lon": 139.763}},
           "floors": [{"level": 1, "plan": "plan.geojson", "rooms": []}]}
    fc = gg.generate(normalise(raw, "t", base_dir=tmp_path), footprint)
    names = {f["properties"]["name"] for f in fc["features"] if f["properties"].get("shape_type") == "poi"}
    assert names == {"inside", "close"}
    assert gg.POI_FOOTPRINT_MARGIN_M == 5.0
