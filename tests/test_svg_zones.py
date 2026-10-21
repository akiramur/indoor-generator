import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec_ = importlib.util.spec_from_file_location("svg_zones", ROOT / "tools" / "svg_zones.py")
zones = importlib.util.module_from_spec(spec_)
spec_.loader.exec_module(zones)

NS = 'xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100"'
BLUE = "rgb(10%, 20%, 90%)"
ARC = "M 10 10 C 30 0 50 0 70 10 L 70 40 C 50 30 30 30 10 40 Z"


def write(tmp_path, body):
    p = tmp_path / "chart.svg"
    p.write_text(f'<svg {NS}>{body}</svg>')
    return str(p)


def test_zone_found_and_legend_swatch_dropped(tmp_path):
    svg = write(tmp_path, f'<path fill="{BLUE}" d="{ARC}"/><rect x="80" y="80" width="19" height="19" fill="{BLUE}"/>')
    by_fill = zones.zones_from_svg(svg)
    assert list(by_fill) == ["#1a33e5"] and len(by_fill["#1a33e5"]) == 1
    z = zones.build_trace(by_fill, {"#1a33e5": {"name": "指定席C", "tier": 1}}, 2.0, 150, "seating", "s1", False)
    assert len(z) == 1 and z[0]["name"] == "指定席C" and z[0]["tier"] == 1 and z[0]["category"] == "seating"
    xs = [p[0] for p in z[0]["px"]]
    assert 18 <= min(xs) and max(xs) <= 142   # SVG x 10..70 doubled (the curve stays within the end points' hull)


def test_unnamed_fill_skipped_unless_kept(tmp_path):
    by_fill = zones.zones_from_svg(write(tmp_path, f'<path fill="{BLUE}" d="{ARC}"/>'))
    assert zones.build_trace(by_fill, {}, 1.0, 150, "seating", "s1", False) == []
    kept = zones.build_trace(by_fill, {}, 1.0, 150, "seating", "s1", True)
    assert kept[0]["name"] == "zone-1a33e5"


def test_pdftocairo_blend_groups_are_hoisted(tmp_path):
    # a zone only inside a compositing group in <defs>, shown through a mask + filter and an opposite page translate
    svg = write(tmp_path, f'<defs><g id="compositing-group-2" transform="translate(75, 64)">'
                          f'<path fill="{BLUE}" d="{ARC}"/></g></defs>'
                          f'<g filter="url(#f)" transform="translate(-75, -64)"><rect width="100" height="100"/></g>')
    by_fill = zones.zones_from_svg(svg)
    (poly,) = by_fill["#1a33e5"]
    assert poly.bounds[0] == 10.0 and poly.bounds[2] == 70.0   # the two translates cancel
