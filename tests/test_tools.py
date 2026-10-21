"""The helper scripts in tools/ (small synthetic images, no network) and `main.py trace` with point entries."""
import argparse
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

import main as cli

TOOLS = Path(__file__).resolve().parent.parent / "tools"


def load_tool(name: str):
    spec = importlib.util.spec_from_file_location(f"tool_{name}", TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def run_tool(monkeypatch):
    def run(name: str, *argv: str):
        monkeypatch.setattr("sys.argv", [name, *argv])
        load_tool(name).main()
    return run


def _map_image(path: Path) -> Path:
    """White 300x200 with a solid blue square (elevator-like, 24 px) and a ringed circle (a round pictogram)."""
    im = Image.new("RGB", (300, 200), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([40, 50, 63, 73], fill=(0, 101, 172))
    d.rectangle([48, 58, 55, 66], fill="white")  # the glyph
    d.ellipse([200, 100, 252, 152], fill=(40, 40, 40))
    d.ellipse([212, 112, 240, 140], fill="white")
    im.save(path)
    return path


def test_detect_squares_finds_the_blue_square_only(tmp_path, run_tool):
    img = _map_image(tmp_path / "1F.png")
    out = tmp_path / "found.json"
    run_tool("detect_pictograms", str(img), "--squares", "0,101,172", "--out", str(out), "--sheet", str(tmp_path / "sheets"))
    hits = json.loads(out.read_text())[str(img)]
    assert [(h["x"], h["y"]) for h in hits] == [(52, 62)]
    assert (tmp_path / "sheets" / "1F.png").exists()


def test_detect_templates_match_a_round_pictogram(tmp_path, run_tool):
    img = _map_image(tmp_path / "1F.png")
    out = tmp_path / "found.json"
    # the legend is the image itself: the ringed circle at (226, 126) is the template and is found again
    run_tool("detect_pictograms", str(img), "--legend", str(img), "--template", "dot=226,126", "--radius", "30",
             "--circle-radius", "26", "--out", str(out))
    hits = json.loads(out.read_text())[str(img)]
    assert any(h["icon"] == "dot" and abs(h["x"] - 226) <= 3 and abs(h["y"] - 126) <= 3 for h in hits)


def test_overlay_and_crop_grid_write_images(tmp_path, run_tool):
    img = _map_image(tmp_path / "1F.png")
    trace = tmp_path / "1F-trace.json"
    trace.write_text(json.dumps([{"id": "L1-stairs-01", "name": "階段", "px": [[40, 50], [63, 50], [63, 73], [40, 73]]},
                                 {"id": "L1-pict-01", "name": "EV", "icon": "elevator", "point": [226, 126]}]))
    run_tool("overlay_trace", str(img), str(trace), "--out", str(tmp_path / "ov.png"))
    ov = np.array(Image.open(tmp_path / "ov.png"))
    assert ov.shape[:2] == (200, 300) and (ov[..., 0] > 200).any() and (ov[..., 1] < 80).any()  # red was drawn
    run_tool("crop_grid", str(img), "20", "20", "100", "80", "--out", str(tmp_path / "grid.png"), "--zoom", "2")
    assert Image.open(tmp_path / "grid.png").size == (200, 160)


def test_add_points_to_a_trace_file_then_replace(tmp_path, run_tool):
    trace = tmp_path / "B1F-trace.json"
    trace.write_text(json.dumps([{"id": "L-1-shop", "name": "Shop", "px": [[0, 0], [1, 0], [1, 1]]}]))
    pts = tmp_path / "pts.json"
    pts.write_text(json.dumps([{"icon": "elevator", "x": 10, "y": 20}, {"icon": "toilet-male", "x": 30, "y": 40},
                               {"name": "階段", "px": [[1, 1], [5, 1], [5, 9], [1, 9]]}]))
    run_tool("add_points", "trace", str(trace), str(pts), "--source", "s1")
    got = json.loads(trace.read_text())
    assert [e["id"] for e in got] == ["L-1-shop", "L-1-pict-01", "L-1-pict-02", "L-1-stairs-01"]
    assert (got[1]["name"], got[1]["icon"], got[1]["point"], got[1]["source"]) == ("エレベーター", "elevator", [10.0, 20.0], "s1")
    assert got[3]["category"] == "connector"
    pts.write_text(json.dumps([{"icon": "atm", "x": 1, "y": 2}]))
    with pytest.raises(SystemExit):
        run_tool("add_points", "trace", str(trace), str(pts))  # L-1-pict-01 already there: needs --replace
    run_tool("add_points", "trace", str(trace), str(pts), "--replace")
    assert [e["id"] for e in json.loads(trace.read_text())] == ["L-1-shop", "L-1-pict-01"]


def test_add_points_to_a_plan_through_the_corners(tmp_path, run_tool):
    plan = tmp_path / "2F.geojson"
    corners = [[139.0, 35.0], [139.001, 35.0], [139.001, 34.999], [139.0, 34.999]]  # image 100x50 px, y down = south
    plan.write_text(json.dumps({"type": "FeatureCollection", "metadata": {"basemap": {"corners": corners}}, "features": []}))
    pts = tmp_path / "pts.json"
    pts.write_text(json.dumps([{"icon": "escalator", "x": 50, "y": 25}]))
    run_tool("add_points", "plan", str(plan), str(pts), "--size", "100x50")
    f = json.loads(plan.read_text())["features"][0]
    assert f["geometry"]["type"] == "Point"
    assert f["geometry"]["coordinates"] == pytest.approx([139.0005, 34.9995])
    assert (f["properties"]["id"], f["properties"]["icon"], f["properties"]["confidence"]) == ("L2-pict-01", "escalator", "sourced")


def test_add_points_from_detect_output(tmp_path, run_tool):
    detect = tmp_path / "found.json"
    detect.write_text(json.dumps({"maps/1F.png": [{"icon": "?", "x": 5, "y": 6}, {"icon": "?", "x": 7, "y": 8}]}))
    trace = tmp_path / "1F-trace.json"
    trace.write_text("[]")
    run_tool("add_points", "trace", str(trace), "--from-detect", str(detect), "--accept", "1:toilet")
    assert json.loads(trace.read_text())[0]["point"] == [7.0, 8.0]


def test_trace_turns_point_entries_into_point_pois(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "configs"
    plan_dir = cfg_dir / "plans" / "t"
    (plan_dir / "maps").mkdir(parents=True)
    box = [[139.0, 35.0], [139.001, 35.0], [139.001, 34.999], [139.0, 34.999], [139.0, 35.0]]
    (plan_dir / "footprint.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [box]}}]}))
    Image.new("RGB", (100, 50), "white").save(plan_dir / "maps" / "1F.png")
    cfg = {"slug": "t", "building": {"name": "t", "source": {"type": "geojson", "path": "plans/t/footprint.geojson"}},
           "floors": [{"level": 1, "rooms": [], "basemap": {"image": "plans/t/maps/1F.png", "corners": box[:4]}}]}
    (cfg_dir / "t.json").write_text(json.dumps(cfg))
    rooms = tmp_path / "1F-trace.json"
    rooms.write_text(json.dumps([
        {"id": "L1-a", "name": "A", "category": "retail", "px": [[10, 10], [40, 10], [40, 30], [10, 30]]},
        {"id": "L1-pict-01", "name": "エレベーター", "icon": "elevator", "point": [50, 25]},
        {"id": "L1-pict-02", "name": "ATM", "icon": "atm", "point": [500, 25]}]))  # far outside the footprint
    monkeypatch.setattr(cli, "_out_dir", lambda slug: tmp_path / "output" / slug)
    rc = cli.cmd_trace(argparse.Namespace(config=str(cfg_dir / "t.json"), level=1, rooms=str(rooms)))
    assert rc == 0
    fc = json.loads((plan_dir / "1F.geojson").read_text())
    by = {f["properties"]["id"]: f for f in fc["features"]}
    assert by["L1-a"]["geometry"]["type"] == "Polygon"
    assert by["L1-pict-01"]["geometry"]["type"] == "Point" and by["L1-pict-01"]["properties"]["icon"] == "elevator"
    assert by["L1-pict-01"]["geometry"]["coordinates"] == pytest.approx([139.0005, 34.9995], abs=2e-6)
    assert by["L1-pict-01"]["properties"]["confidence"] == "sourced"
