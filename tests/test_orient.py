"""orient.py: which way a floor map is turned, from names on the map vs OSM names (synthetic, no network)."""
from indoor_gen import orient, quality

LON, LAT = 139.70, 35.69
M = 1 / 111_320.0  # degrees of latitude per metre


def square(half_m=100.0):
    k = 1 / __import__("math").cos(__import__("math").radians(LAT))
    d, e = half_m * M, half_m * M * k
    return [[LON - e, LAT + d], [LON + e, LAT + d], [LON + e, LAT - d], [LON - e, LAT - d]]  # TL TR BR BL


SIZE = (1000, 1000)
NAMED = [("カフェ ひなた", (100, 100)), ("Books ABC", (900, 150)), ("ラーメン 太郎", (500, 800))]


def places_for(corners):
    return [{"name": n, "lnglat": list(orient.px_to_lnglat(corners, SIZE, px))} for n, px in NAMED]


def test_names_tell_which_quarter_turn_puts_the_map_right():
    truth = square()
    places = places_for(truth)
    # the map is placed correctly: turn 0 is best
    rows = orient.score_turns(truth, SIZE, NAMED, places)
    v = orient.verdict(rows)
    assert v["status"] == "clear" and v["best_turn_deg"] == 0
    # the same map placed a quarter turn counter-clockwise (270 clockwise off): turning it 90° clockwise puts it right
    wrong = orient.turned(truth, 3)
    v = orient.verdict(orient.score_turns(wrong, SIZE, NAMED, places))
    assert v["status"] == "clear" and v["best_turn_deg"] == 90
    upside = orient.turned(truth, 2)
    assert orient.verdict(orient.score_turns(upside, SIZE, NAMED, places))["best_turn_deg"] == 180


def test_too_few_matching_names_leave_it_undecided():
    truth = square()
    rows = orient.score_turns(truth, SIZE, NAMED, places_for(truth)[:1])
    assert orient.verdict(rows)["status"] == "undecided"
    assert orient.verdict(orient.score_turns(truth, SIZE, NAMED, []))["best_turn_deg"] is None


def test_names_match_ignoring_width_spaces_and_case_but_not_short_fragments():
    assert orient.same_name("ＢＯＯＫＳ ＡＢＣ", "books abc") and orient.same_name("カフェ ひなた", "カフェひなた 新宿店")
    assert not orient.same_name("A1", "A10") and not orient.same_name("", "x")


def test_zone_centres_skip_placeholders_and_icons_and_placement_basis_reads_the_alignment():
    trace = [{"name": "(名称なし)", "px": [[0, 0], [2, 0], [2, 2]]}, {"name": "トイレ", "icon": "toilet", "px": [[0, 0]]},
             {"name": "店", "px": [[0, 0], [10, 0], [10, 10], [0, 10]]}]
    assert orient.zone_centres(trace) == [("店", (5.0, 5.0))]
    rows = quality.placement_basis({"floors": [
        {"name": "1F", "basemap": {"alignment": {"pairs": [1, 2, 3], "rms_m": 0.7}}}, {"name": "2F", "basemap": {"image": "x"}},
        {"name": "3F"}]})
    assert [(r["floor"], r["basis"]) for r in rows] == [("1F", "landmarks"), ("2F", "outline")]
    assert "landmark pairs" in quality.format_placement(rows)
