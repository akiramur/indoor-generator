"""Draw a floor's traced rooms and pictogram points on its source image, to check a trace by eye.

Reads the `*-trace.json` that `main.py trace` turns into a plan ([{id, name, ..., px: [[x, y], ...]}] for rooms,
[{id, name, icon, point: [x, y]}] for pictograms) and writes the image with every outline / point drawn and labelled.

    .venv/bin/python tools/overlay_trace.py <image> <floor>-trace.json --out /tmp/overlay.png
        [--only stairs,pict]      only ids containing one of these words
        [--crop X0,Y0,X1,Y1]      crop (image pixels) and enlarge, e.g. one corner of a big map
        [--scale 2]               enlargement of the output (default 1; 2 with --crop)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw

Image.MAX_IMAGE_PIXELS = None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", type=Path)
    ap.add_argument("trace", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--only", help="comma-separated words; keep entries whose id contains one")
    ap.add_argument("--crop", help="X0,Y0,X1,Y1 in image pixels")
    ap.add_argument("--scale", type=float)
    args = ap.parse_args()

    img = Image.open(args.image).convert("RGB")
    x0 = y0 = 0
    if args.crop:
        x0, y0, x1, y1 = (int(v) for v in args.crop.split(","))
        img = img.crop((x0, y0, x1, y1))
    scale = args.scale or (2.0 if args.crop else 1.0)
    if scale != 1:
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    draw = ImageDraw.Draw(img)
    to_out = lambda p: ((p[0] - x0) * scale, (p[1] - y0) * scale)  # noqa: E731
    words = args.only.split(",") if args.only else None
    n = 0
    for r in json.loads(args.trace.read_text(encoding="utf-8")):
        if words and not any(w in r["id"] for w in words):
            continue
        if "point" in r:
            x, y = to_out(r["point"])
            draw.ellipse([x - 12, y - 12, x + 12, y + 12], outline=(255, 0, 0), width=2)
            draw.text((x + 14, y - 6), f"{r['id'].split('-')[-1]} {r.get('icon', '')}", fill=(200, 0, 0))
        else:
            pts = [to_out(p) for p in r["px"]]
            draw.polygon(pts, outline=(255, 0, 0))
            draw.text((pts[0][0] + 2, pts[0][1] + 2), r["id"].split("-", 1)[-1], fill=(200, 0, 0))
        n += 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    img.save(args.out)
    print(f"{n} entries drawn -> {args.out}")


if __name__ == "__main__":
    main()
