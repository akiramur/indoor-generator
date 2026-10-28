from indoor_gen import stacking

LON, LAT, D = 139.76, 35.68, 0.0001  # ~9 m per step east, ~11 m per step north


def pt(icon, level, x, y):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [LON + x * D, LAT + y * D]},
            "properties": {"shape_type": "poi", "type": "room", "icon": icon, "level": level}}


def floor(level, name):
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[LON, LAT], [LON + 5 * D, LAT],
            [LON + 5 * D, LAT + 5 * D], [LON, LAT]]]}, "properties": {"type": "floor", "level": level, "name": name}}


def fc(features):
    fp = {"type": "Polygon", "coordinates": [[[LON, LAT], [LON + 5 * D, LAT], [LON + 5 * D, LAT + 5 * D], [LON, LAT]]]}
    return {"type": "FeatureCollection", "features": features, "metadata": {"footprint": fp, "crs": "auto"}}


SHAFTS = [("elevator", 0, 0), ("stairs", 2, 0), ("elevator", 1, 3), ("escalator", 3, 3)]


def test_floors_with_the_same_shafts_are_stacked():
    feats = [floor(1, "1F"), floor(2, "2F")] + [pt(i, 1, x, y) for i, x, y in SHAFTS] + [pt(i, 2, x, y) for i, x, y in SHAFTS]
    (row,) = stacking.report(fc(feats))
    assert row["floors"] == ["1F", "2F"] and row["agree_now"] == 4 and row["verdict"] == "stacked"


def test_a_floor_shifted_as_a_whole_is_reported_with_its_offset():
    feats = [floor(1, "1F"), floor(2, "2F")] + [pt(i, 1, x, y) for i, x, y in SHAFTS] \
        + [pt(i, 2, x + 0.6, y - 0.4) for i, x, y in SHAFTS]      # ~ 7 m east, ~ 4 m south
    (row,) = stacking.report(fc(feats))
    assert row["agree_now"] == 0 and row["best_matches"] == 4 and 6 < row["offset_m"] < 9
    assert "m off" in row["verdict"]


def test_a_floor_with_a_single_shaft_cannot_tell():
    feats = [floor(1, "1F"), floor(2, "2F"), pt("elevator", 1, 0, 0), pt("stairs", 1, 2, 0), pt("elevator", 2, 0, 0)]
    (row,) = stacking.report(fc(feats))
    assert row["verdict"] == "too few shafts to tell"
    assert "no floor" in stacking.format_report([])
