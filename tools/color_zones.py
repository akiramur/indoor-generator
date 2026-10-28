"""Shop outlines from the colour fills of a raster floor map (a map that colours its tenants, e.g. red = restaurants,
teal = shops) -> a floor's `*-trace.json` (the pixel outlines `main.py trace` turns into the plan).

Each fill colour is a mask; thin separators between neighbouring shops (white lines a few pixels wide) are cut by an
erosion before the pieces are labelled, then each piece is grown back inside the mask. Number tiles and text inside a
shop do not make holes: the outline is the outer contour.

    .venv/bin/python tools/color_zones.py map.jpg 1F-trace.json \\
        --fill 238,129,129,12,restaurant --fill 92,188,188,12,retail --names names.json --id-prefix L1-z --source s1

`--fill R,G,B[,tolerance[,category]]` (tolerance per channel, default 14). `--names` is a JSON list
`[{"x": 227, "y": 328, "name": "サザコーヒー", "category": "restaurant"}]`: a piece takes the name of the point inside it
(or the nearest within --max-name-dist px); a piece with no name is kept as "(名称なし)" unless --drop-unnamed.
`--sheet out.png` draws the outlines on the image with their ids, to check them.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from shapely.geometry import Point, Polygon


def fill_small_holes(mask: np.ndarray, hole_area: int) -> np.ndarray:
    """Fill the islands of non-mask pixels smaller than `hole_area` that do not touch the border: number tiles,
    shop names and pictograms drawn inside a shop. A separator line reaches the outside, so it is not an island."""
    n, lab, stats, _ = cv2.connectedComponentsWithStats((1 - mask).astype(np.uint8))
    out = mask.copy()
    h, w = mask.shape
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if area < hole_area and x > 0 and y > 0 and x + bw < w and y + bh < h:
            out[lab == i] = 1
    return out


def pieces(mask: np.ndarray, erode: int, min_area: int, hole_area: int = 3000) -> list[np.ndarray]:
    """Masks of the pieces of `mask`, separators thinner than 2*erode pixels cut."""
    mask = fill_small_holes(mask, hole_area)
    k = np.ones((2 * erode + 1, 2 * erode + 1), np.uint8)
    core = cv2.erode(mask, k) if erode else mask
    n, lab = cv2.connectedComponents(core)
    out = []
    for i in range(1, n):
        piece = (lab == i).astype(np.uint8)
        if erode:
            piece = cv2.dilate(piece, k) & mask
        if piece.sum() >= min_area:
            out.append(piece)
    return out


def outline(mask: np.ndarray, close: int) -> np.ndarray:
    """Outer contour of `mask` after a closing of `close` pixels (fills the notches a number tile cuts at a shop's edge)."""
    if close:
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((close, close), np.uint8))
    cs, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    c = max(cs, key=cv2.contourArea)
    return cv2.approxPolyDP(c, max(1.5, 0.01 * cv2.arcLength(c, True)), True)[:, 0, :]


# bits of a line that are directions, not a name ("JR有楽町駅 スターバックス" is one OCR line)
DIRECTION = re.compile(r"[↑↓←→↗↙↖↘✕]\s*\S*|JR\S*?駅|\S*方面")
# text that is not a shop name: floor labels (B3-6F, F-5F), the legend, titles
NOT_A_NAME = re.compile(r"^[B]?\d*F?[-~〜][B]?\d*F$|^[B]?\d+F$|グルメ|ショップ$|レストラン|ラーメン激戦区|^(テラス|アトリウム)$|フロア|テレキューブ")
TILE = re.compile(r"\d{1,2}")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", DIRECTION.sub(" ", unicodedata.normalize("NFKC", text))).strip()


def name_zones(zones: list[dict], lines: list[dict], tile_radius: float = 115.0, min_confidence: float = 0.3) -> dict:
    """{zone id: {"name", "confidence", "lines"}} from OCR lines (see tools/ocr_text.py).

    A line whose centre is inside a shop belongs to it. A line outside every shop (labels often sit beside the shop,
    with a leader line) belongs to the shop of the nearest number tile (a 1-2 digit line) within `tile_radius` pixels,
    the tile's own shop being the one it stands in. Lines are joined top to bottom, left to right."""
    polys = [(z["id"], Polygon(z["px"]).buffer(0)) for z in zones]

    def centre(line):
        return Point(line["x"] + line["w"] / 2, line["y"] + line["h"] / 2)

    def shop_at(p, reach=0.0):
        hit = [(g.area, zid) for zid, g in polys if g.contains(p)]
        if hit:
            return min(hit)[1]
        near = sorted((g.distance(p), zid) for zid, g in polys)
        return near[0][1] if near and near[0][0] <= reach else None

    tiles = []
    for line in lines:
        text = _clean(line["text"])
        if TILE.fullmatch(text):
            zid = shop_at(centre(line), reach=40.0)
            if zid:
                tiles.append((centre(line), zid))
    owned: dict[str, list[dict]] = {}
    for line in lines:
        text = _clean(line["text"])
        if (TILE.fullmatch(text) or len(text) < 2 or line.get("confidence", 1) < min_confidence
                or NOT_A_NAME.search(text)):
            continue
        c = centre(line)
        zid = shop_at(c)
        if zid is None and tiles:
            d, zid = min(((c.distance(tp), z) for tp, z in tiles), default=(None, None))
            if d is None or d > tile_radius:
                zid = None
        if zid:
            owned.setdefault(zid, []).append(line)
    out = {}
    for zid, ls in owned.items():
        ls.sort(key=lambda t: (round((t["y"] + t["h"] / 2) / 22), t["x"]))
        name = re.sub(r"\s+", " ", " ".join(_clean(t["text"]) for t in ls)).strip()
        out[zid] = {"name": name, "confidence": round(min(t.get("confidence", 1) for t in ls), 2), "lines": len(ls)}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--fill", action="append", required=True, metavar="R,G,B[,tol[,category]]")
    ap.add_argument("--names", type=Path)
    ap.add_argument("--ocr", action="store_true",
                    help="name the shops by reading the map's text (tools/ocr_text.py); names from --names win; "
                         "the result is a candidate: check it against the map")
    ap.add_argument("--id-prefix", default="z")
    ap.add_argument("--source")
    ap.add_argument("--erode", type=int, default=2, help="half-width in pixels of the separators to cut (default 2)")
    ap.add_argument("--min-area", type=int, default=250, help="smallest piece in pixels (default 250)")
    ap.add_argument("--hole-area", type=int, default=3000,
                    help="islands smaller than this inside a shop (number tiles, text) are filled (default 3000)")
    ap.add_argument("--max-name-dist", type=float, default=40.0)
    ap.add_argument("--close", type=int, default=25, help="closing in pixels applied to each shop's outline (default 25)")
    ap.add_argument("--drop-unnamed", action="store_true")
    ap.add_argument("--sheet", type=Path)
    a = ap.parse_args()

    rgb = np.asarray(Image.open(a.image).convert("RGB")).astype(int)
    names = json.loads(a.names.read_text(encoding="utf-8")) if a.names else []
    zones = []  # {"category", "mask"}
    for spec in a.fill:
        parts = spec.split(",")
        colour = np.array([int(x) for x in parts[:3]])
        tol = int(parts[3]) if len(parts) > 3 and parts[3] else 14
        category = parts[4] if len(parts) > 4 else "retail"
        mask = (np.abs(rgb - colour) <= tol).all(axis=-1).astype(np.uint8)
        for piece in pieces(mask, a.erode, a.min_area, a.hole_area):
            zones.append({"category": category, "mask": piece})
    # a piece takes the name whose point is in it (or nearest, within --max-name-dist)
    for z in zones:
        inv = cv2.distanceTransform(1 - z["mask"], cv2.DIST_L2, 3)
        best = None
        for nm in names:
            x, y = int(nm["x"]), int(nm["y"])
            d = 0.0 if z["mask"][y, x] else float(inv[y, x])
            if best is None or d < best[0]:
                best = (d, nm)
        z["name"], z["dist"] = (best[1]["name"], best[0]) if best and best[0] <= a.max_name_dist else (None, None)
        if z["name"]:
            z["category"] = best[1].get("category", z["category"])
    if a.ocr:  # shops no point named: take the name from the map's own text
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import ocr_text

        lines = ocr_text.ocr_lines(a.image)
        prov = [{"id": f"p{i}", "px": outline(z["mask"], a.close).tolist()} for i, z in enumerate(zones)]
        found = name_zones(prov, lines)
        for i, z in enumerate(zones):
            hit = found.get(f"p{i}")
            if hit and not z["name"]:
                z["name"], z["ocr_confidence"] = hit["name"], hit["confidence"]
        # a tiny unnamed piece touching a named shop of the same colour is a fragment of it (a number tile cut it off)
        grown = [cv2.dilate(z["mask"], np.ones((13, 13), np.uint8)) for z in zones]
        for i, z in enumerate(zones):
            if z["name"] or int(z["mask"].sum()) > 1500:
                continue
            near = [j for j, w in enumerate(zones) if w["name"] and w["category"] == z["category"]
                    and (grown[i] & w["mask"]).any()]
            if near:
                z["name"] = zones[min(near, key=lambda j: -int((grown[i] & zones[j]["mask"]).sum()))]["name"]
    # pieces of one shop (a number tile or a label cut it in two) are joined
    merged = {}
    for z in zones:
        key = z["name"] or f"#{id(z)}"
        if key in merged:
            merged[key]["mask"] = merged[key]["mask"] | z["mask"]
            if z.get("ocr_confidence") is not None:
                merged[key]["ocr_confidence"] = min(z["ocr_confidence"], merged[key].get("ocr_confidence", 1))
        else:
            merged[key] = dict(z)
    zones = list(merged.values())
    zones.sort(key=lambda z: (np.where(z["mask"])[0].min() // 40, np.where(z["mask"])[1].min()))
    out = []
    for z in zones:
        if not z["name"]:
            if a.drop_unnamed:
                continue
            z["name"] = "(名称なし)"
        z["px"] = outline(z["mask"], a.close).tolist()
        out.append(z)
    for i, z in enumerate(out, 1):
        entry = {"id": f"{a.id_prefix}{i:02d}", "name": z["name"], "category": z["category"]}
        if a.source:
            entry["source"] = a.source
        z.update(entry)
    final = [{k: z[k] for k in ("id", "name", "category", *(["source"] if a.source else []), "px")} for z in out]
    a.out.write_text(json.dumps(final, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(final)} zones -> {a.out}")
    for z in final:
        px = np.array(z["px"])
        note = f"  [ocr {z['ocr_confidence']}]" if z.get("ocr_confidence") is not None else ""
        print(f"  {z['id']}  {z['category']:<10} {len(px):>2} pts  area {cv2.contourArea(px.astype(np.float32)):7.0f} px²  {z['name']}{note}")
    if a.sheet:
        from PIL import ImageDraw

        im = Image.open(a.image).convert("RGB")
        d = ImageDraw.Draw(im)
        for z in final:
            pts = [tuple(p) for p in z["px"]]
            d.line(pts + [pts[0]], fill=(0, 0, 0), width=2)
            c = np.mean(np.array(z["px"]), axis=0)
            d.text((c[0] - 8, c[1] - 6), z["id"].split("-")[-1], fill=(0, 0, 160))
        im.save(a.sheet)


if __name__ == "__main__":
    main()
