import pytest
from shapely.geometry import box

from indoor_gen.importers import common


@pytest.mark.parametrize("name,level", [("1F", 1), ("12F", 12), ("B1F", -1), ("b2", -2)])
def test_floor_level(name, level):
    assert common.floor_level(name) == level


def _analysis(name, outline, fit2):
    return {"floor": {"name": name}, "outline": outline, "fit2": fit2, "t_svg_local": name,
            "report": {"warnings": []}}


def test_share_frames_uses_the_best_fit_for_floors_drawn_in_the_same_frame():
    best = _analysis("1F", box(0, 0, 100, 60), 0.95)
    same = _analysis("2F", box(1, 0, 101, 60), 0.5)
    other = _analysis("3F", box(0, 0, 40, 20), 0.5)
    common.share_frames([same, best, other])
    assert same["t_svg_local"] == "1F" and same["report"]["map_frame_from"] == "1F"
    assert other["t_svg_local"] == "3F" and other["report"]["warnings"]  # poor fit, different frame: warned


def test_debug_svg_draws_footprint_and_labels():
    svg = common.debug_svg(box(0, 0, 10, 10), [("7", box(1, 1, 4, 4))])
    assert svg.startswith("<svg") and ">7</text>" in svg
