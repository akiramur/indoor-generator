import pytest
from shapely.geometry import Point, box

from indoor_gen import align, georef

PX = [(0, 0), (800, 0), (800, 600), (120, 450)]


def _truth(xy_t):
    return [georef.apply(Point(p), xy_t).coords[0] for p in PX]


def test_similarity_recovers_flip_rotation_scale():
    # Image (y down) -> map: flip, rotate 30°, 0.05 m/px, shift.
    truth = georef.compose(georef.FLIP_Y, georef.similarity(0.05, 30, (0, 0), 12, -7))
    xy = _truth(truth)
    t = align.fit_pairs(PX[:2], xy[:2])
    assert max(align.residuals(t, PX, xy)) < 1e-6
    for got, want in zip(t, truth):
        assert got == pytest.approx(want, abs=1e-9)


def test_similarity_least_squares_reports_the_bad_point():
    truth = georef.compose(georef.FLIP_Y, georef.similarity(0.05, -12, (0, 0), 3, 4))
    xy = _truth(truth)
    xy[3] = (xy[3][0] + 5, xy[3][1])  # mis-clicked by 5 m
    res = align.residuals(align.fit_pairs(PX, xy), PX, xy)
    assert res.index(max(res)) == 3
    assert max(res) > 2


def test_affine_handles_uneven_scale():
    truth = georef.compose(georef.FLIP_Y, [0.05, 0.0, 0.0, 0.08, 0.0, 0.0], georef.similarity(1, 20, (0, 0), 1, 2))
    xy = _truth(truth)
    assert max(align.residuals(align.fit_pairs(PX, xy, "affine"), PX, xy)) < 1e-6
    # A similarity cannot fit it exactly.
    assert max(align.residuals(align.fit_pairs(PX, xy), PX, xy)) > 1


def test_too_few_pairs():
    with pytest.raises(ValueError):
        align.fit_pairs(PX[:2], PX[:2], "affine")
    with pytest.raises(ValueError):
        align.fit_pairs([(5, 5), (5, 5)], [(0, 0), (1, 1)])


def test_initial_transform_fits_inside_target_with_top_facing_map_up():
    target = box(-50, -20, 50, 20)
    t = align.initial_transform(target, 400, 300, "north")
    tl, tr, br, bl = align.corners(t, 400, 300)
    assert tl.y > bl.y and tr.x > tl.x          # top edge to the north, not mirrored
    placed = box(*georef.apply(box(0, 0, 400, 300), t).bounds)
    assert placed.within(target.buffer(1e-6))
    assert placed.centroid.distance(target.centroid) < 1e-6
    tl, tr, _, _ = align.corners(align.initial_transform(target, 400, 300, "west"), 400, 300)
    assert tl.x < tr.x + 1e-9 and abs(tl.x - tr.x) < 1e-6 and tl.y < tr.y  # top edge runs along the west side


def test_apply_keeps_auto_corners_and_reset_restores_them(tmp_path, monkeypatch):
    import json

    import main

    auto = [[139.0, 35.1], [139.1, 35.1], [139.1, 35.0], [139.0, 35.0]]
    cfg = tmp_path / "b.json"
    cfg.write_text(json.dumps({"floors": [{"level": 1, "rooms": [], "basemap": {"image": "m.png", "corners": auto, "source": "s1"}}]}))
    monkeypatch.setattr(main, "cmd_build", lambda args: print('{"ok": true}') or 0)
    fitted = [[139.2, 35.2]] * 4

    def apply():
        result = {"warnings": [], "basemap": {"image": "m.png", "corners": fitted, "alignment": {"mode": "similarity"}}}
        assert main._apply_basemap(cfg, 1, result) == 0
        return json.loads(cfg.read_text())["floors"][0]["basemap"]

    bm = apply()
    assert bm["corners"] == fitted and bm["auto_corners"] == auto and bm["source"] == "s1"
    assert apply()["auto_corners"] == auto          # a second alignment keeps the first placement
    assert main._reset_basemap(cfg, 1) == 0
    assert json.loads(cfg.read_text())["floors"][0]["basemap"] == {"image": "m.png", "corners": auto, "source": "s1"}
    assert main._reset_basemap(cfg, 1) == 1         # nothing left to reset


def _drawing(tmp_path, size=(400, 300)):
    """A white page with an L-shaped drawn building (so it has a unique orientation), plus a tiny detached label."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(im)
    d.rectangle([40, 60, 300, 140], fill=(120, 120, 120))
    d.rectangle([40, 140, 120, 260], fill=(120, 120, 120))
    d.rectangle([380, 280, 390, 290], fill=(0, 0, 0))
    path = tmp_path / "plan.png"
    im.save(path)
    return path, georef.apply(box(40, 60, 300, 140).union(box(40, 140, 120, 260)), georef.FLIP_Y)


def test_auto_fit_recovers_scale_turn_and_position(tmp_path):
    path, drawn = _drawing(tmp_path)
    # The real building: the drawing, at 0.2 m/px, turned 20° and moved. Nothing is bent.
    truth = georef.compose(georef.FLIP_Y, georef.similarity(0.2, 20, (0, 0), 30, -15))
    target = georef.apply(box(40, 60, 300, 140).union(box(40, 140, 120, 260)), truth)
    t, iou, _ = align.auto_fit(path, target, map_up="north")
    assert iou > 0.95
    placed = georef.apply(box(40, 60, 300, 140).union(box(40, 140, 120, 260)), t)
    assert placed.symmetric_difference(target).area / target.area < 0.08
    assert t[0] ** 2 + t[2] ** 2 == pytest.approx(t[1] ** 2 + t[3] ** 2, rel=1e-6)   # a similarity: no shear / uneven scale


def test_auto_fit_finds_the_quarter_turn_when_map_up_is_unknown(tmp_path):
    path, _ = _drawing(tmp_path)
    shape = box(40, 60, 300, 140).union(box(40, 140, 120, 260))
    truth = georef.compose(georef.FLIP_Y, georef.orientation(1), georef.similarity(0.2, 0, (0, 0), 5, 9))
    target = georef.apply(shape, truth)
    _, iou, _ = align.auto_fit(path, target)
    assert iou > 0.95


def test_outline_ignores_a_grey_page_background(tmp_path):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (300, 200), (247, 247, 247))   # not pure white, like a map page
    ImageDraw.Draw(im).rectangle([50, 40, 150, 120], fill=(255, 255, 255), outline=(120, 120, 120))
    path = tmp_path / "page.png"
    im.save(path)
    page, _ = align.image_outline(path, page_colour=True)
    assert page.area < 0.3 * 300 * 200            # just the drawn building
    whole, _ = align.image_outline(path)          # default: only pure white counts as blank
    assert whole.area > 0.9 * 300 * 200


def _long_bar(tmp_path):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (600, 120), "white")
    ImageDraw.Draw(im).rectangle([20, 30, 580, 90], fill=(90, 90, 90))
    path = tmp_path / "bar.png"
    im.save(path)
    return path


def test_auto_fit_says_so_when_the_shape_cannot_tell_which_way_is_up(tmp_path):
    path = _long_bar(tmp_path)
    target = box(-100, -10, 100, 10)       # a symmetric bar: upside down fits as well as upright
    _, _, notes = align.auto_fit(path, target)
    assert notes["ambiguous"]
    assert len(notes["candidates"]) >= 2


def test_auto_fit_takes_the_turn_of_a_floor_already_placed(tmp_path):
    path = _long_bar(tmp_path)
    target = box(-100, -10, 100, 10)
    t0, _, _ = align.auto_fit(path, target)
    flipped = (align._image_angle(t0) + 180) % 360
    t, _, notes = align.auto_fit(path, target, like_angle=flipped)
    assert "ambiguous" not in notes
    assert align._angle_gap(align._image_angle(t), flipped) < 20


def test_auto_fit_starts_from_the_north_arrow_and_settles_the_symmetric_case(tmp_path):
    path = _long_bar(tmp_path)
    target = box(-100, -10, 100, 10)
    for north in (10, 190):
        t, _, notes = align.auto_fit(path, target, north_cw=north)
        assert "ambiguous" not in notes
        assert notes["north_cw"] == north
        # The bar fits at every half turn; the one nearest the arrow wins (image-x angle = north_cw here).
        assert align._angle_gap(align._image_angle(t), north) < 20
        assert notes["off_north_deg"] < 20


def test_north_arrow_is_read_in_true_compass_terms_when_the_frame_is_turned(tmp_path):
    path = _long_bar(tmp_path)
    target = box(-100, -10, 100, 10)       # the building's own axes: the frame is turned 25° from east
    t, _, notes = align.auto_fit(path, target, north_cw=205, frame_angle=25)
    assert align._angle_gap(align._image_angle(t), 180) < 20       # 205 - 25
    assert notes["north_cw"] == 205


def test_tile_map_outline_reads_the_building_out_of_a_map_screenshot(tmp_path):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (800, 500), (244, 244, 247))               # apron: no blank page to cut away
    d = ImageDraw.Draw(im)
    d.rectangle([0, 380, 800, 400], fill=(255, 255, 255))             # a white runway line: not the building
    d.polygon([(100, 100), (700, 400), (680, 430), (80, 130)], fill=(255, 253, 245))   # cream floor
    d.rectangle([300, 40, 330, 110], fill=(255, 253, 245))
    path = tmp_path / "tile.png"
    im.save(path)
    outline, size = align.tile_map_outline(path)
    assert size == (800.0, 500.0)
    assert 0.02 < outline.area / (800 * 500) < 0.2
    assert outline.bounds[0] < 120 and outline.bounds[2] > 650        # both ends of the building
    assert outline.bounds[3] < 450                                    # not the runway line


def test_true_width_keeps_the_drawings_size_when_the_footprint_is_too_big(tmp_path):
    from shapely import affinity
    path, _ = _drawing(tmp_path)
    shape = box(40, 60, 300, 140).union(box(40, 140, 120, 260))
    truth = georef.compose(georef.FLIP_Y, georef.similarity(0.2, 20, (0, 0), 30, -15))
    real = georef.apply(shape, truth)
    # An outdated footprint, twice as wide and long as the real building (OSM still holds the pre-construction building).
    old = affinity.scale(real, 2, 2, origin=real.centroid)

    def size(t):                       # metres per pixel of a similarity
        return (t[0] ** 2 + t[2] ** 2) ** 0.5

    t_free, _, _ = align.auto_fit(path, old, map_up="north")
    assert size(t_free) == pytest.approx(0.4, rel=0.1)      # stretched to the wrong footprint

    # The drawn outline is 261 px wide (PIL's rectangle includes both ends): 52.2 m at 0.2 m/px.
    t, iou, notes = align.auto_fit(path, old, map_up="north", true_width_m=52.2)
    assert size(t) == pytest.approx(0.2, rel=1e-6)          # kept exactly
    assert notes["m_per_px"] == pytest.approx(0.2, abs=1e-4) and notes["true_width_m"] == 52.2
    placed = georef.apply(shape, t)
    assert placed.area == pytest.approx(real.area, rel=1e-6)
    assert placed.intersection(old).area / placed.area > 0.99   # inside the footprint (where exactly is up to the overlap)
    assert 0.2 < iou < 0.35                                   # the overlap is bounded by the area ratio (about 0.25)
    assert t[0] ** 2 + t[2] ** 2 == pytest.approx(t[1] ** 2 + t[3] ** 2, rel=1e-6)   # still no shear


def test_true_width_must_be_positive(tmp_path):
    path, _ = _drawing(tmp_path)
    with pytest.raises(ValueError):
        align.auto_fit(path, box(0, 0, 50, 50), map_up="north", true_width_m=0)
    with pytest.raises(ValueError):
        align.auto_fit(path, box(0, 0, 50, 50), map_up="north", true_width_m=-3)
