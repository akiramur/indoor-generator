import json

from indoor_gen.compare import compare

LON, LAT, D = 139.76, 35.68, 0.0005  # ~45 m x 55 m footprint


def box(x0, x1):
    lon0, lon1 = LON + x0 * D, LON + x1 * D
    return {"type": "Polygon", "coordinates": [[[lon0, LAT], [lon1, LAT], [lon1, LAT + D], [lon0, LAT + D], [lon0, LAT]]]}


def room(rid, name, category, x0, x1, confidence="sourced", geometry_source="vector"):
    return {"type": "Feature", "geometry": box(x0, x1),
            "properties": {"id": rid, "name": name, "type": "room", "level": 1, "floor_name": "1F",
                           "category": category, "area_m2": (x1 - x0) * 2500, "confidence": confidence,
                           "geometry_source": geometry_source}}


def write(out, rooms, cost_usd):
    out.mkdir(parents=True)
    fc = {"type": "FeatureCollection", "features": rooms,
          "metadata": {"building": "t", "footprint": box(0, 1), "crs": "auto", "basemaps": []}}
    (out / "indoor.geojson").write_text(json.dumps(fc), encoding="utf-8")
    (out / "cost.json").write_text(json.dumps({"usd": cost_usd, "turns": 3,
                                               "models": {"m": {"output": 100}}}), encoding="utf-8")


def test_compare_reports_cost_evidence_and_agreement(tmp_path):
    write(tmp_path / "a", [room("a1", "カフェ", "food", 0, 0.5), room("a2", "店舗A", "retail", 0.5, 1)], 10.0)
    write(tmp_path / "b", [room("b1", "カフェ ", "food", 0, 0.5),
                           room("b2", "オフィス", "office", 0.5, 1, "estimated", "ratio")], 4.0)
    r = compare(tmp_path / "a", tmp_path / "b")
    assert (r["a"]["cost_usd"], r["b"]["cost_usd"]) == (10.0, 4.0)
    assert r["a"]["sourced_area_pct"] == 100 and r["b"]["sourced_area_pct"] == 50
    assert r["b"]["geometry_source"] == {"vector": 1, "ratio": 1}
    floor = r["floors"]["1F"]
    assert floor["category_agreement_pct"] == 50.0
    assert (floor["matched_rooms"], floor["matched_same_name"]) == (2, 1)
    assert r["category_agreement_pct"] == 50.0
