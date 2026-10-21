"""Trim a floor-map image to the part around the building.

Official floor maps often carry a legend, other buildings or a wide blank margin, which the preview would draw
across the map. The shown image is cut to the floor's extent plus a margin; the original stays the source for
alignment (`source_url`), so the point pairs keep their pixel coordinates.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

from . import align, georef

MARGIN_RATIO = 0.05   # of the floor's longer side
MIN_MARGIN_M = 8.0
TRIM_BELOW = 0.9      # leave the image alone if the crop would keep more than this share of it


def crop_rect(corners_local: list, size: tuple[int, int], region: BaseGeometry) -> tuple[int, int, int, int] | None:
    """Pixel rectangle (x0, y0, x1, y1) around `region` (local metres), or None if there is nothing worth trimming.

    `corners_local` are the image's TL, TR, BR, BL in local metres; the map between pixels and metres is affine."""
    w, h = size
    to_px = align.fit_pairs(corners_local, [(0, 0), (w, 0), (w, h), (0, h)], "affine")
    if region.is_empty:
        return None
    minx, miny, maxx, maxy = region.bounds
    margin = max(MIN_MARGIN_M, MARGIN_RATIO * max(maxx - minx, maxy - miny))
    # The rectangle that holds the buffered region in pixels (the image may be rotated against the frame).
    xs, ys = georef.apply(region.buffer(margin).convex_hull, to_px).exterior.coords.xy
    x0, y0 = max(0, int(min(xs))), max(0, int(min(ys)))
    x1, y1 = min(w, int(max(xs)) + 1), min(h, int(max(ys)) + 1)
    if x1 - x0 < 2 or y1 - y0 < 2:   # the floor is not on the image: nothing sensible to keep
        return None
    if (x1 - x0) * (y1 - y0) > TRIM_BELOW * w * h:
        return None
    return x0, y0, x1, y1


def cropped_basemap(image: Path, corners: list, frame, region_wgs84: BaseGeometry,
                    cache_dir: Path) -> tuple[Path, list, tuple[int, int, int, int]] | None:
    """Cut the image to the floor's extent. Returns (png path, [lng, lat] corners TL, TR, BR, BL, pixel rect),
    or None when there is nothing to trim or the image cannot be cut (e.g. an SVG)."""
    from PIL import Image

    try:
        with Image.open(image) as im:
            source = im.convert("RGBA")
    except OSError:
        return None
    w, h = source.size
    corners_local = [frame.to_local(Point(c)).coords[0] for c in corners]
    rect = crop_rect(corners_local, (w, h), frame.to_local(region_wgs84))
    if rect is None:
        return None
    x0, y0, x1, y1 = rect
    to_local = align.fit_pairs([(0, 0), (w, 0), (w, h), (0, h)], corners_local, "affine")
    new = [[round(c, 7) for c in frame.to_wgs84(georef.apply(Point(p), to_local)).coords[0]]
           for p in [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]]
    key = hashlib.sha1(json.dumps([hashlib.sha1(image.read_bytes()).hexdigest(), rect]).encode()).hexdigest()[:16]
    out = cache_dir / f"cropped-{key}.png"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        source.crop(rect).save(out)
    return out, new, rect
