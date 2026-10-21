"""Find facility pictograms (escalator, elevator, stairs, ATM, toilet, ...) in floor-map images that have no vector data.

Candidates only: look at the contact sheets, then write the accepted positions as `point` entries of the floor's
`*-trace.json` (see the indoor-floor-image skill). Output: JSON {image: [{icon, x, y, ...}]} (pixel centres) and, with
--sheet DIR, one contact sheet per image (each candidate cropped, numbered, with its icon and position).

Round pictograms (a circle with a glyph), by FFT correlation; the legend shows each pictogram once:
  --template name=x,y  cut a named pictogram out of --legend (its centre) and match it at several sizes inside circles
                       found by a ring test (a closed dark ring with a clear outside)
  --toilets            circles holding both a red and a blue figure (the men's / women's toilet), by colour

Square pictograms (a solid coloured square with a white glyph):
  --squares R,G,B[,tol[,min,max]]  connected blobs of that colour (tolerance per channel, default 30) whose box is
                                   min..max pixels (default 14..48) and mostly filled; the icon is left as "?" for you
                                   to name from the sheet

    .venv/bin/python tools/detect_pictograms.py configs/plans/<slug>/maps/{1F,2F}.png \\
        --legend configs/plans/<slug>/maps/1F.png --template atm=91,477 --template elevator=91,1292 --toilets \\
        --min-x 340 --out /tmp/found.json --sheet /tmp/sheets
    .venv/bin/python tools/detect_pictograms.py 1F.png 2F.png --squares 0,101,172 --out /tmp/found.json --sheet /tmp/sheets
"""

from __future__ import annotations

import argparse
import json
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

SCALES = (0.8, 0.9, 1.0, 1.15, 1.3, 1.5, 1.7)
TOILET_SCALES = (0.9, 1.0, 1.2, 1.4, 1.6, 1.8)


def correlate(image: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """Cross-correlation of image with kernel, 'valid' positions only (value at a window's top-left corner)."""
    h, w = kernel.shape
    H, W = image.shape
    f = np.fft.rfft2(image, s=(H + h, W + w))
    g = np.fft.rfft2(kernel[::-1, ::-1], s=(H + h, W + w))
    return np.fft.irfft2(f * g, s=(H + h, W + w))[h - 1:H, w - 1:W]


def disk(n: int, r0: float, r1: float) -> np.ndarray:
    """An n x n mask of the ring r0 <= distance from the centre <= r1."""
    y, x = np.mgrid[:n, :n] - (n - 1) / 2
    d = np.hypot(x, y)
    return ((d >= r0) & (d <= r1)).astype(np.float32)


def ink(img: Image.Image) -> np.ndarray:
    """1 where a pixel is not near white (line art, glyphs, filled circles)."""
    return (np.array(img.convert("RGB")).astype(np.int16).min(axis=2) < 200).astype(np.float32)


def suppress(found: list[tuple], min_x: int, min_dist: float, xy: slice = slice(-2, None)) -> list[tuple]:
    """Best-first, dropping anything left of min_x (the legend) or within min_dist of a kept hit."""
    kept: list[tuple] = []
    for hit in sorted(found, reverse=True):
        x, y = hit[xy]
        if x < min_x:
            continue
        if all((x - k[xy][0]) ** 2 + (y - k[xy][1]) ** 2 > min_dist**2 for k in kept):
            kept.append(hit)
    return kept


def match_templates(img: Image.Image, templates: dict[str, np.ndarray], R: int, circle_r: float,
                    min_score: float, min_x: int) -> list[dict]:
    """templates: name -> ink mask of a (2R x 2R) legend crop. Pictograms are circles of radius circle_r in the legend."""
    P = ink(img)
    found = []
    for sc in SCALES:
        n = int(round(2 * R * sc)) | 1
        rr = circle_r * sc
        inner, ring, outside = disk(n, 0, rr * 0.7), disk(n, rr * 0.8, rr * 0.98), disk(n, rr * 1.12, rr * 1.3)
        ring_fill = correlate(P, ring) / ring.sum()
        out_clear = 1 - correlate(P, outside) / outside.sum()
        pre = (ring_fill > 0.8) & (out_clear > 0.45)
        if not pre.any():
            continue
        ni = inner.sum()
        s1, s2 = correlate(P, inner), correlate(P * P, inner)
        var = np.maximum(s2 - s1 * s1 / ni, 1e-6)
        ys, xs = np.nonzero(pre)
        for name, crop in templates.items():
            t = np.array(Image.fromarray((crop * 255).astype(np.uint8)).resize((n, n), Image.LANCZOS)).astype(np.float32) / 255
            tz = (t - (t * inner).sum() / ni) * inner
            tn = np.sqrt((tz**2).sum())
            num = correlate(P, tz)
            for y, x in zip(ys, xs):
                score = num[y, x] / (tn * np.sqrt(var[y, x]))
                if score > min_score:
                    found.append((float(score), name, sc, int(x + n // 2), int(y + n // 2)))
    return [{"icon": n, "x": x, "y": y, "score": round(s, 3), "scale": sc}
            for s, n, sc, x, y in suppress(found, min_x, 36, slice(3, 5))]


def match_toilets(img: Image.Image, circle_r: float, min_x: int) -> list[dict]:
    """Circles with a red and a blue figure inside."""
    a = np.array(img.convert("RGB")).astype(np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    dark = ((r < 110) & (g < 110) & (b < 110)).astype(np.float32)
    red = ((r > 170) & (g < 90) & (b < 90)).astype(np.float32)
    blue = ((r < 60) & (g > 110) & (b > 170)).astype(np.float32)
    found = []
    for sc in TOILET_SCALES:
        rr = circle_r * sc
        n = int(2 * rr * 1.3) | 1
        ring, inner = disk(n, rr * 0.8, rr * 0.98), disk(n, 0, rr * 0.75)
        ring_dark = correlate(dark + red + blue, ring) / ring.sum()
        sr, sb = correlate(red, inner), correlate(blue, inner)
        ys, xs = np.nonzero((ring_dark > 0.8) & (sr > 25 * sc * sc) & (sb > 25 * sc * sc))
        found += [(float(min(sr[y, x], sb[y, x])), sc, int(x + n // 2), int(y + n // 2)) for y, x in zip(ys, xs)]
    return [{"icon": "toilet", "x": x, "y": y, "score": round(s, 1), "scale": sc}
            for s, sc, x, y in suppress(found, min_x, 45)]


def match_squares(img: Image.Image, color: tuple[int, int, int], tol: int, lo: int, hi: int, min_x: int) -> list[dict]:
    """Blobs of a solid colour whose bounding box is a roughly square lo..hi pixels and mostly filled."""
    a = np.array(img.convert("RGB")).astype(np.int16)
    mask = np.all(np.abs(a - np.array(color)) <= tol, axis=2)
    mask[:, :min_x] = False
    H, W = mask.shape
    seen = np.zeros_like(mask)
    hits = []
    for y0, x0 in zip(*np.nonzero(mask)):
        if seen[y0, x0]:
            continue
        queue, n = deque([(y0, x0)]), 0
        seen[y0, x0] = True
        x_min = x_max = x0
        y_min = y_max = y0
        while queue:
            y, x = queue.popleft()
            n += 1
            x_min, x_max, y_min, y_max = min(x_min, x), max(x_max, x), min(y_min, y), max(y_max, y)
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1)):
                yy, xx = y + dy, x + dx
                if 0 <= yy < H and 0 <= xx < W and mask[yy, xx] and not seen[yy, xx]:
                    seen[yy, xx] = True
                    queue.append((yy, xx))
        w, h = x_max - x_min + 1, y_max - y_min + 1
        if lo <= w <= hi and lo <= h <= hi and n / (w * h) > 0.4:
            hits.append({"icon": "?", "x": int(x_min + w // 2), "y": int(y_min + h // 2), "w": int(w), "h": int(h),
                         "fill": round(n / (w * h), 2)})
    return sorted(hits, key=lambda h: (h["y"] // 60, h["x"]))


def contact_sheet(img: Image.Image, hits: list[dict], dest: Path) -> None:
    cols = 8
    rows = max(1, (len(hits) + cols - 1) // cols)
    sheet = Image.new("RGB", (cols * 130, rows * 150), "white")
    draw = ImageDraw.Draw(sheet)
    img = img.convert("RGB")
    for i, h in enumerate(hits):
        ox, oy = (i % cols) * 130, (i // cols) * 150
        sheet.paste(img.crop((h["x"] - 60, h["y"] - 60, h["x"] + 60, h["y"] + 60)), (ox + 5, oy))
        draw.text((ox + 2, oy + 122), f"{i} {h['icon']} ({h['x']},{h['y']})", fill=(200, 0, 0))
    sheet.save(dest)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("images", nargs="+", type=Path)
    ap.add_argument("--legend", type=Path, help="image the templates are cut from (usually one of the maps)")
    ap.add_argument("--template", action="append", default=[], metavar="NAME=X,Y",
                    help="a legend pictogram by its icon name and centre pixel; repeat")
    ap.add_argument("--toilets", action="store_true", help="also find red + blue toilet circles by colour")
    ap.add_argument("--squares", metavar="R,G,B[,TOL[,MIN,MAX]]", help="find solid-colour square pictograms (see above)")
    ap.add_argument("--radius", type=int, default=30, help="half-size of the template crop in legend pixels")
    ap.add_argument("--circle-radius", type=float, default=26, help="circle radius in the legend, pixels")
    ap.add_argument("--min-score", type=float, default=0.45)
    ap.add_argument("--min-x", type=int, default=0, help="ignore hits left of this x (the legend column)")
    ap.add_argument("--max-x", type=int, help="ignore hits right of this x (a legend column on the right)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sheet", type=Path, help="directory for one contact sheet per image")
    args = ap.parse_args()

    if not (args.template or args.toilets or args.squares):
        ap.error("give --template, --toilets and/or --squares")
    templates = {}
    if args.template:
        if not args.legend:
            ap.error("--template needs --legend")
        legend = Image.open(args.legend)
        for spec in args.template:
            name, xy = spec.split("=")
            cx, cy = (int(v) for v in xy.split(","))
            R = args.radius
            templates[name] = ink(legend.crop((cx - R, cy - R, cx + R, cy + R)))
    result = {}
    for path in args.images:
        img = Image.open(path)
        hits = match_templates(img, templates, args.radius, args.circle_radius, args.min_score, args.min_x) if templates else []
        if args.toilets:
            hits += match_toilets(img, args.circle_radius, args.min_x)
        if args.squares:
            v = [int(t) for t in args.squares.split(",")]
            hits += match_squares(img, tuple(v[:3]), v[3] if len(v) > 3 else 30,
                                  v[4] if len(v) > 4 else 14, v[5] if len(v) > 5 else 48, args.min_x)
        if args.max_x:
            hits = [h for h in hits if h["x"] <= args.max_x]
        result[str(path)] = hits
        print(f"{path}: {len(hits)} candidates")
        if args.sheet:
            args.sheet.mkdir(parents=True, exist_ok=True)
            contact_sheet(img, hits, args.sheet / f"{path.stem}.png")
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
