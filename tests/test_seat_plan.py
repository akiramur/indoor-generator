import pytest
import importlib.util
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec_ = importlib.util.spec_from_file_location("gen_seat_plan", ROOT / "tools" / "gen_seat_plan.py")
gen = importlib.util.module_from_spec(spec_)
spec_.loader.exec_module(gen)

SPEC = {
    "origin": [139.0, 35.0], "audience_bearing_deg": 90.0, "stage": {"width": 40.0, "depth": 10.0},
    "blocks": [{"id": "b", "section": "1階席 中央", "seat_pitch": 0.5, "u_center": 0.0, "seats": [17, 28],
                "row_groups": [{"rows": [1, 3], "v0": 2.0, "pitch": 1.0}, {"rows": [4, 5], "v0": 8.0, "pitch": 1.0}]}],
}


def seats(fc):
    return [f for f in fc["features"] if f["properties"].get("icon") == "seat"]


def metres(a, b):
    return ((b[0] - a[0]) * 111320 * math.cos(math.radians(35.0)), (b[1] - a[1]) * 110574)


def test_one_point_per_seat_with_row_and_number():
    fc = gen.build(SPEC, "ホールC", "舞台", "s1")
    s = seats(fc)
    assert len(s) == 5 * 12
    p = s[0]["properties"]
    assert (p["row"], p["number"], p["icon"], p["section"]) == (1, 17, "seat", "1階席 中央")
    assert p["facing_deg"] == 270.0   # audience bearing 90 -> the seats face west, toward the stage
    assert len({f["properties"]["id"] for f in s}) == len(s)
    labels = [f["properties"] for f in fc["features"] if f["properties"].get("icon") == "row-label"]
    assert len(labels) == 5 * 2 and {p["row_label"] for p in labels} == {"left", "right"}   # a number at each end of each row
    assert [f["properties"]["id"] for f in fc["features"] if f["geometry"]["type"] == "Polygon"] == ["stage", "seating"]


def test_rows_run_away_from_the_stage_and_numbers_to_the_audiences_right():
    # audience bearing 90 (east): the stage is to the west, rows go east; the audience faces west, so its right is north
    fc = gen.build(SPEC, "ホールC", "舞台", None)
    by = {(f["properties"]["row"], f["properties"]["number"]): f["geometry"]["coordinates"] for f in seats(fc)}
    dx, dy = metres(by[(1, 17)], by[(2, 17)])
    assert dx == pytest.approx(1.0, abs=0.01) and abs(dy) < 0.01   # next row: 1 m further east
    dx, dy = metres(by[(1, 17)], by[(1, 18)])
    assert dy == pytest.approx(0.5, abs=0.01) and abs(dx) < 0.01   # next number: 0.5 m north
    dx, dy = metres(by[(3, 17)], by[(4, 17)])
    assert dx == pytest.approx(4.0, abs=0.01)   # the aisle between the row groups (v 4 m -> 8 m)


def test_explicit_rows_are_evenly_spaced_fans_that_turn_with_the_row():
    spec = {"origin": [139.0, 35.0], "audience_bearing_deg": 90.0, "stage": {"width": 40.0, "depth": 10.0},
            "blocks": [{"id": "l", "section": "左", "row_labels": ["left"], "rows": [
                {"row": 1, "from": [-6.0, 3.0], "to": [-3.0, 3.0], "numbers": [13, 16], "not_fixed": [13]},
                {"row": 2, "from": [-6.0, 4.0], "to": [-3.0, 5.0], "numbers": [14, 16]}]}]}
    fc = gen.build(spec, "ホールC", "舞台", None)
    s = {(f["properties"]["row"], f["properties"]["number"]): f for f in seats(fc)}
    assert len(s) == 4 + 3
    assert [k for k, f in s.items() if f["properties"].get("fixed") is False] == [(1, 13)]
    # row 1 is straight: it faces the stage; row 2 runs down toward the rear to the right, so it faces a little right of it
    assert s[(1, 13)]["properties"]["facing_deg"] == 270.0
    assert s[(2, 14)]["properties"]["facing_deg"] != 270.0
    x = [s[(1, n)]["geometry"]["coordinates"][1] for n in (13, 14, 15, 16)]
    assert (x[1] - x[0]) == pytest.approx(x[2] - x[1], abs=2e-8)   # even spacing (north = audience's right here)
    labels = [f["properties"]["row_label"] for f in fc["features"] if f["properties"].get("icon") == "row-label"]
    assert labels == ["left", "left"]


def test_a_balcony_spec_can_leave_out_the_rooms_and_tags_seats_with_their_tier():
    spec = {**SPEC, "stage_room": False, "seating_room": False,
            "blocks": [{**SPEC["blocks"][0], "tier": 2}]}
    fc = gen.build(spec, "ホールC", "舞台", None)
    assert [f for f in fc["features"] if f["geometry"]["type"] == "Polygon"] == []
    assert {f["properties"]["tier"] for f in seats(fc)} == {2}
    assert "tier" not in seats(gen.build(SPEC, "ホールC", "舞台", None))[0]["properties"]   # 1階席: no tier, the default colour
