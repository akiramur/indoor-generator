"""Crop part of a floor-map image, enlarge it and draw a pixel grid (labelled in ORIGINAL image pixels), to read coordinates.

    .venv/bin/python tools/crop_grid.py <image> X0 Y0 WIDTH HEIGHT --out /tmp/crop.png [--zoom 2] [--step 50]

Lines every --step pixels (every 2nd one stronger and labelled). Read a point as (label x, label y) of the original image;
that is what the `px` / `point` of a `*-trace.json` use.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

Image.MAX_IMAGE_PIXELS = None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", type=Path)
    ap.add_argument("x0", type=int)
    ap.add_argument("y0", type=int)
    ap.add_argument("width", type=int)
    ap.add_argument("height", type=int)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--zoom", type=float, default=2.0)
    ap.add_argument("--step", type=int, default=50)
    a = ap.parse_args()

    crop = Image.open(a.image).convert("RGB").crop((a.x0, a.y0, a.x0 + a.width, a.y0 + a.height))
    z = a.zoom
    crop = crop.resize((round(a.width * z), round(a.height * z)), Image.LANCZOS)
    d = ImageDraw.Draw(crop)
    for gx in range((a.x0 // a.step + 1) * a.step, a.x0 + a.width, a.step):
        X, big = (gx - a.x0) * z, (gx // a.step) % 2 == 0
        d.line([(X, 0), (X, crop.height)], fill=(255, 0, 0) if big else (255, 170, 170))
        if big:
            d.text((X + 2, 2), str(gx), fill=(200, 0, 0))
    for gy in range((a.y0 // a.step + 1) * a.step, a.y0 + a.height, a.step):
        Y, big = (gy - a.y0) * z, (gy // a.step) % 2 == 0
        d.line([(0, Y), (crop.width, Y)], fill=(0, 0, 255) if big else (170, 170, 255))
        if big:
            d.text((2, Y + 2), str(gy), fill=(0, 0, 200))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    crop.save(a.out)
    print(f"-> {a.out}")


if __name__ == "__main__":
    main()
