import pytest
from shapely.geometry import Polygon

from indoor_gen import georef

L = Polygon([(0, 0), (60, 0), (60, 20), (20, 20), (20, 50), (0, 50)])


def test_fit_recovers_rotation_scale_and_mirror_free_transform():
    truth = georef.compose(georef.orientation(1), georef.similarity(2.5, 3.0, (0, 0), 100, -40))
    target = georef.apply(L, truth)
    t, score = georef.fit(L, target)
    assert score > 0.97
    assert georef.apply(L, t).symmetric_difference(target).area / target.area < 0.05


def test_fit_with_known_map_up_and_image_coordinates():
    # An image (y down) whose top edge faces west.
    image = georef.apply(L, georef.FLIP_Y)
    truth = georef.compose(georef.FLIP_Y, georef.orientation(georef.MAP_UP_TURNS["west"]),
                           georef.similarity(0.1, 0, (0, 0), 500, 800))
    target = georef.apply(image, truth)
    t, score = georef.fit(image, target, base=georef.FLIP_Y, orientations=[(georef.MAP_UP_TURNS["west"], False)])
    assert score > 0.97


def test_compose_order():
    a = georef.similarity(2, 0, (0, 0), 0, 0)
    b = georef.similarity(1, 0, (0, 0), 10, 0)
    p = Polygon([(0, 0), (1, 0), (1, 1)])
    assert georef.apply(p, georef.compose(a, b)).bounds[0] == pytest.approx(10)


def test_fit_fixed_scale_turns_and_moves_but_never_rescales():
    from shapely.geometry import box

    from indoor_gen import georef

    outline = box(0, 0, 100, 60)                 # drawn 100 x 60 units
    target = box(500, 700, 500 + 10, 700 + 6)    # a footprint far smaller than the drawing at 0.1 m/unit... it is 10 x 6 m
    t, iou = georef.fit_fixed_scale(outline, target, 0.05)   # at 0.05 m/unit the drawing is 5 x 3 m, inside the footprint
    placed = georef.apply(outline, t)
    assert round(placed.bounds[2] - placed.bounds[0], 2) == 5.0 and round(placed.bounds[3] - placed.bounds[1], 2) == 3.0
    assert target.contains(placed) and 0.2 < iou < 0.3
    free, _ = georef.fit(outline, target)        # the free fit stretches it to the footprint
    assert round(georef.apply(outline, free).area, 1) > 40
