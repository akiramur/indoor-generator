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
    rc = cli.cmd_trace(argparse.Namespace(config=str(cfg_dir / "t.json"), level=1, rooms=str(rooms), listed=13))
    assert rc == 0
    fc = json.loads((plan_dir / "1F.geojson").read_text())
    assert fc["metadata"]["tenants_listed"] == 13        # what the source's shop list gives, for `report`
    by = {f["properties"]["id"]: f for f in fc["features"]}
    assert by["L1-a"]["geometry"]["type"] == "Polygon"
    assert by["L1-pict-01"]["geometry"]["type"] == "Point" and by["L1-pict-01"]["properties"]["icon"] == "elevator"
    assert by["L1-pict-01"]["geometry"]["coordinates"] == pytest.approx([139.0005, 34.9995], abs=2e-6)
    assert by["L1-pict-01"]["properties"]["confidence"] == "sourced"


def test_color_zones_cuts_neighbours_fills_number_tiles_and_names_the_shops(tmp_path, run_tool):
    """Two red shops side by side with a thin white separator, a white number tile inside the first (a hole, not a cut),
    a teal shop: three named outlines."""
    im = Image.new("RGB", (400, 200), (248, 248, 248))
    d = ImageDraw.Draw(im)
    red, teal = (238, 129, 129), (92, 188, 188)
    d.rectangle((20, 20, 119, 119), fill=red)
    d.rectangle((122, 20, 221, 119), fill=red)        # a 2 px white line at x = 120-121 separates the two reds
    d.rectangle((50, 40, 76, 66), fill=(255, 255, 255))   # a number tile inside the first shop
    d.rectangle((260, 20, 359, 119), fill=teal)
    image, trace, names = tmp_path / "map.png", tmp_path / "trace.json", tmp_path / "names.json"
    im.save(image)
    names.write_text(json.dumps([{"x": 60, "y": 100, "name": "A", "category": "restaurant"},
                                 {"x": 170, "y": 100, "name": "B", "category": "restaurant"},
                                 {"x": 300, "y": 100, "name": "C", "category": "retail"}]), encoding="utf-8")
    run_tool("color_zones", str(image), str(trace), "--fill", "238,129,129,14,restaurant", "--fill",
             "92,188,188,14,retail", "--names", str(names), "--id-prefix", "L1-t", "--source", "s1")
    zones = json.loads(trace.read_text(encoding="utf-8"))
    assert [z["name"] for z in zones] == ["A", "B", "C"] and [z["category"] for z in zones] == ["restaurant"] * 2 + ["retail"]
    from shapely.geometry import Polygon
    areas = [Polygon(z["px"]).area for z in zones]
    assert 9000 < areas[0] < 10200 and 9000 < areas[1] < 10200 and 9000 < areas[2] < 10200   # ~100 x 100 px each: the tile is filled
    assert Polygon(zones[0]["px"]).intersection(Polygon(zones[1]["px"])).area < 400            # cut at the separator


def test_name_zones_ties_ocr_lines_to_shops_inside_them_and_through_number_tiles():
    """Text inside a shop is its name; text outside goes to the shop of the nearest number tile; directions, floor labels and
    the legend are not names (no OCR engine needed: the lines are given)."""
    cz = load_tool("color_zones")

    def box(x0, y0, x1, y1):
        return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]

    def line(text, x, y, w=60, h=14, conf=1.0):
        return {"text": text, "confidence": conf, "x": x - w / 2, "y": y - h / 2, "w": w, "h": h}

    zones = [{"id": "z1", "px": box(0, 0, 100, 100)}, {"id": "z2", "px": box(200, 0, 300, 100)},
             {"id": "z3", "px": box(0, 300, 100, 400)}]
    lines = [line("タリーズ", 50, 60), line("コーヒー", 50, 78), line("&TEA", 50, 94),        # inside z1, two lines
             line("01", 30, 30, 20), line("02", 230, 30, 20), line("03", 30, 330, 20),         # number tiles, one per shop
             line("治一郎", 330, 40),                                                           # outside z2, near tile 02
             line("JR有楽町駅 スターバックス", 130, 340),                                       # outside z3: direction + a name
             line("↑東京ビル", 150, 200), line("B3-6F", 150, 230), line("グルメ", 150, 260),      # not names
             line("まだ遠い", 600, 600, conf=0.9),                                              # far from every tile
             line("かすれ", 330, 60, conf=0.1)]                                                # unreadable
    got = cz.name_zones(zones, lines, tile_radius=120)
    assert got["z1"]["name"] == "タリーズ コーヒー &TEA" and got["z1"]["lines"] == 3
    assert got["z2"]["name"] == "治一郎"
    assert got["z3"]["name"] == "スターバックス"             # the direction is cut off the line
    assert set(got) == {"z1", "z2", "z3"}


def test_ocr_text_falls_back_when_no_engine_is_there(monkeypatch, tmp_path):
    ot = load_tool("ocr_text")
    monkeypatch.setattr(ot, "_vision_binary", lambda: None)
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(ot.OcrUnavailable):
        ot.ocr_lines(tmp_path / "x.png")
    rows = "level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext\n" \
           "5\t1\t1\t1\t1\t1\t10\t20\t30\t12\t91.5\tKITTE\n5\t1\t1\t1\t1\t2\t50\t20\t30\t12\t-1\t \n"
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/tesseract")
    monkeypatch.setattr(ot.subprocess, "run", lambda cmd, **k: type("R", (), {"stdout": "eng\njpn\n" if "--list-langs" in cmd else rows})())
    assert ot.ocr_lines(tmp_path / "x.png") == [{"text": "KITTE", "confidence": 0.915, "x": 10.0, "y": 20.0, "w": 30.0, "h": 12.0}]


# --- pictogram_bank ---------------------------------------------------------------------------------------------------

def _glyph_map(path, glyphs, size=40):
    """A grey map with one black square tile per glyph; each tile has a white bar pattern (a vertical / horizontal / cross)."""
    img = Image.new("RGB", (size * (len(glyphs) + 1), size * 2), (250, 250, 240))
    d = ImageDraw.Draw(img)
    for i, g in enumerate(glyphs):
        x0, y0 = size // 2 + i * size, size // 2
        d.rectangle([x0, y0, x0 + 31, y0 + 31], fill=(36, 24, 21))
        if g in ("v", "x"):
            d.rectangle([x0 + 13, y0 + 4, x0 + 18, y0 + 27], fill="white")
        if g in ("h", "x"):
            d.rectangle([x0 + 4, y0 + 13, x0 + 27, y0 + 18], fill="white")
    img.save(path)
    return [{"x": size // 2 + i * size + 15.5, "y": size // 2 + 15.5, "w": 34, "h": 34} for i in range(len(glyphs))]


def test_pictogram_bank_labels_a_new_map_by_the_nearest_glyph(tmp_path, monkeypatch):
    bank_tool = load_tool("pictogram_bank")
    bank_file = tmp_path / "bank.json"
    first = tmp_path / "a.png"
    hits = _glyph_map(first, "vhx")
    found = tmp_path / "found.json"
    found.write_text(json.dumps({str(first): hits}))
    monkeypatch.setattr("sys.argv", ["pictogram_bank", "--bank", str(bank_file), "add", str(first), str(found),
                                     "--accept", "0:elevator,1:stairs", "--reject", "2"])
    with pytest.raises(SystemExit) as e:
        bank_tool.main()
    assert e.value.code == 0
    entries = bank_tool.load_bank(bank_file)
    assert [e["label"] for e in entries] == ["elevator", "stairs", "other"]
    # the same glyphs read from boxes a couple of pixels off
    img = Image.open(first)
    got = [bank_tool.classify(bank_tool.thumbnail(img, h["x"] + 2, h["y"] - 1, h["w"], h["h"]), entries) for h in hits]
    assert [c["label"] for c in got] == ["elevator", "stairs", "other"]
    assert all(c["agreement"] > 0.9 and c["margin"] > 0.05 for c in got)
    # a glyph unlike all of them is not claimed with confidence
    blank = np.zeros((bank_tool.N, bank_tool.N), bool)
    blank[2:6, 2:20] = True
    assert bank_tool.classify(blank, entries)["agreement"] < 0.9


def test_pictogram_bank_pack_round_trips_and_loo(tmp_path):
    bank_tool = load_tool("pictogram_bank")
    rng = np.random.default_rng(0)
    bits = rng.random((bank_tool.N, bank_tool.N)) > 0.5
    assert (bank_tool.unpack(bank_tool.pack(bits)) == bits).all()
    assert bank_tool.classify(bits, []) == {"label": None, "agreement": 0.0, "margin": 0.0}
    a = np.zeros((bank_tool.N, bank_tool.N), bool)
    a[:, 8:12] = True
    b = np.zeros_like(a)
    b[8:12, :] = True
    entries = [{"label": lab, "map": "m", "bits": bank_tool.pack(g)} for lab, g in
               (("stairs", a), ("stairs", np.roll(a, 1, axis=1)), ("elevator", b), ("elevator", np.roll(b, 1, axis=0)))]
    assert all(bank_tool.classify(bank_tool.unpack(e["bits"]), entries, exclude=i)["label"] == e["label"]
               for i, e in enumerate(entries))
