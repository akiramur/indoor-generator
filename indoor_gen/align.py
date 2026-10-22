"""Place a floor-map image on the map from point pairs clicked in the preview.

Pairs map image pixels (x right, y down) to map positions. They are fitted in
the building's metric local frame, so residuals come out in metres:

- "similarity": scale + rotation + translation (the image's y axis is flipped
  first, so no mirroring). Needs 2 pairs; with more it is a least-squares fit.
- "affine": also allows uneven scale / shear. Needs 3 pairs (used internally, e.g. to read a placed image's corners).

A floor map is never bent to the footprint: it is only scaled, turned and moved. `auto_fit` finds that
placement from the drawing's own outline; point pairs (similarity) are the manual way to correct it.

The result is the image's four corners (top-left, top-right, bottom-right,
bottom-left) in WGS84 — the order a Mapbox image source expects.
"""

from __future__ import annotations

import math

import numpy as np
from shapely.geometry import Point, Polygon
from shapely.geometry import box as rect
from shapely.geometry.base import BaseGeometry

from . import georef

MIN_PAIRS = {"similarity": 2, "affine": 3}


def fit_pairs(px: list[tuple[float, float]], xy: list[tuple[float, float]],
              mode: str = "similarity") -> list[float]:
    """Transform (shapely affine params) taking image pixels `px` onto local metres `xy`."""
    if mode not in MIN_PAIRS:
        raise ValueError(f"unknown mode '{mode}'")
    if len(px) != len(xy) or len(px) < MIN_PAIRS[mode]:
        raise ValueError(f"{mode} needs at least {MIN_PAIRS[mode]} point pairs, got {min(len(px), len(xy))}")
    src = np.asarray(px, dtype=float)
    dst = np.asarray(xy, dtype=float)
    if mode == "affine":
        a = np.column_stack([src, np.ones(len(src))])
        (pa, pb, xo), (pd, pe, yo) = np.linalg.lstsq(a, dst, rcond=None)[0].T
        return [pa, pb, pd, pe, xo, yo]
    # Similarity on complex numbers: w = k * z + c, with z the y-flipped pixel.
    z = src[:, 0] - 1j * src[:, 1]
    w = dst[:, 0] + 1j * dst[:, 1]
    zc, wc = z - z.mean(), w - w.mean()
    if not np.any(np.abs(zc) > 1e-9):
        raise ValueError("the image points coincide")
    k = (wc * np.conj(zc)).sum() / (np.abs(zc) ** 2).sum()
    c = w.mean() - k * z.mean()
    # x' = Re(k)x + Im(k)y + Re(c), y' = Im(k)x - Re(k)y + Im(c)  (for z = x - iy)
    return [k.real, k.imag, k.imag, -k.real, c.real, c.imag]


def residuals(t: list[float], px, xy) -> list[float]:
    """Distance (m) between each fitted image point and the map point it was paired with."""
    return [georef.apply(Point(p), t).distance(Point(q)) for p, q in zip(px, xy)]


def corners(t: list[float], width: float, height: float) -> list[Point]:
    return [georef.apply(Point(x, y), t) for x, y in ((0, 0), (width, 0), (width, height), (0, height))]


def initial_transform(target: BaseGeometry, width: float, height: float, map_up: str = "north") -> list[float]:
    """Rough placement before any points are picked: the image, turned so its top faces
    `map_up`, centred on `target` and scaled to fit inside its bounding box."""
    pre = georef.compose(georef.FLIP_Y, georef.orientation(georef.MAP_UP_TURNS[map_up]))
    box = georef.apply(rect(0, 0, width, height), pre)
    bx0, by0, bx1, by1 = box.bounds
    tx0, ty0, tx1, ty1 = target.bounds
    s = min((tx1 - tx0) / (bx1 - bx0), (ty1 - ty0) / (by1 - by0))
    cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
    move = [s, 0.0, 0.0, s, (tx0 + tx1) / 2 - s * cx, (ty0 + ty1) / 2 - s * cy]
    return georef.compose(pre, move)


def _border_background(rgba: np.ndarray) -> np.ndarray | None:
    """The most common opaque colour along the image's edge (a page that is light grey, not white), or None."""
    edge = np.concatenate([rgba[0], rgba[-1], rgba[:, 0], rgba[:, -1]])
    edge = edge[edge[:, 3] > 128][:, :3]
    if len(edge) == 0:
        return None
    colours, counts = np.unique(edge, axis=0, return_counts=True)
    return colours[counts.argmax()] if counts.max() > 0.5 * len(edge) else None


def image_outline(path, display_size: tuple[float, float] = (0, 0),
                  max_side: int | None = None, page_colour: bool = False) -> tuple[BaseGeometry, tuple[float, float]]:
    """Largest opaque region of the image that is not background, in pixels of the displayed size (the
    image's own size when `display_size` is 0). Background is white, or with `page_colour` the colour
    that fills the image's edge. `max_side` reads a shrunken copy (faster) and scales the outline back."""
    from PIL import Image
    from shapely import affinity
    from shapely.ops import unary_union

    im = Image.open(path).convert("RGBA")
    full = im.size
    if max_side and max(full) > max_side:
        k = max_side / max(full)
        im = im.resize((max(1, round(full[0] * k)), max(1, round(full[1] * k))), Image.BILINEAR)
    rgba = np.asarray(im)
    bg = _border_background(rgba) if page_colour else None
    if bg is None:
        blank = (rgba[..., :3] > 248).all(axis=-1)
    else:
        blank = (np.abs(rgba[..., :3].astype(int) - bg.astype(int)) <= 3).all(axis=-1)
    mask = (rgba[..., 3] > 128) & ~blank
    boxes = []
    for y in range(mask.shape[0]):
        row = np.concatenate(([0], mask[y].astype(np.int8), [0]))
        edges = np.flatnonzero(np.diff(row))
        boxes += [rect(x0, y, x1, y + 1) for x0, x1 in zip(edges[0::2], edges[1::2])]
    if not boxes:
        raise ValueError("the image has no drawn area (all white or transparent)")
    gap = max(1.0, 3 * im.width / full[0])
    # Close thin gaps (wall lines, labels) and keep the biggest piece; detached text drops out.
    merged = unary_union(boxes).buffer(gap, join_style="mitre").buffer(-gap, join_style="mitre")
    biggest = max(getattr(merged, "geoms", [merged]), key=lambda g: g.area)
    outline = Polygon(biggest.exterior).simplify(gap / 3)
    w, h = display_size
    sx = (w or full[0]) / im.width
    sy = (h or full[1]) / im.height
    return affinity.scale(outline, sx, sy, origin=(0, 0)), (w or full[0], h or full[1])


def tile_map_outline(path, max_side: int = 1600) -> tuple[BaseGeometry, tuple[float, float]]:
    """The building in a map-app screenshot (terrain, runways and roads all round it, so there is no blank
    page to cut away): its floor colour (cream) and coloured fills, closed and reduced to the biggest piece.
    In image pixels, y down. Tuned on Platinumaps floor maps."""
    import cv2

    rgb = np.asarray(__import__("PIL.Image", fromlist=["Image"]).open(path).convert("RGB")).astype(int)
    full = rgb.shape[:2][::-1]
    k = min(1.0, max_side / max(full))
    if k < 1.0:
        rgb = cv2.resize(rgb.astype(np.uint8), None, fx=k, fy=k, interpolation=cv2.INTER_AREA).astype(int)
    cream = (rgb[..., 0] >= 250) & (rgb[..., 2] <= 249) & (rgb[..., 0] - rgb[..., 2] >= 5)
    coloured = rgb.max(-1) - rgb.min(-1) >= 18
    m = (cream | coloured).astype(np.uint8)
    r = max(3, int(round(25 * k * 2)))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((r, r), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m)
    if n < 2:
        raise ValueError("no building floor colour found in the image")
    mask = (lab == 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))).astype(np.uint8)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    outline = Polygon(max(contours, key=cv2.contourArea)[:, 0, :] / k).buffer(0).simplify(4 / k)
    return outline, (float(full[0]), float(full[1]))


AMBIGUOUS_IOU = 0.05    # two distinct orientations closer than this in overlap cannot be told apart
START_STEP_DEG = 30


def _image_angle(t: list[float]) -> float:
    """Direction (deg, counter-clockwise from +x of the local frame) that the image's x axis points to."""
    return math.degrees(math.atan2(t[2], t[0])) % 360


def _angle_gap(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


def fit_orientations(path, target: BaseGeometry, map_up: str | None = None, around: float | None = None) -> list[dict]:
    """Every distinct placement the drawing settles into, best first: [{"angle", "iou", "t"}].

    The drawing's outline is fitted to `target` (local metres) by overlap from starts all around the circle
    (or only near `map_up`'s quarter turn); it is only scaled, turned and moved. A building that is nearly
    symmetric (long and thin) gives two placements 180 degrees apart with almost the same overlap."""
    outline, _ = image_outline(path, max_side=800, page_colour=True)
    return fit_outline(outline, target, map_up, around)


def fit_outline(outline: BaseGeometry, target: BaseGeometry, map_up: str | None = None,
                around: float | None = None, scale: float | None = None) -> list[dict]:
    """`fit_orientations` for an outline already read (in image pixels, y down). `around` is an image-x angle
    (see `_image_angle`) to start near, like `map_up` but for any angle. `scale` (metres per pixel) fixes the
    drawing's size: it is then only turned and moved, never rescaled to the target."""
    if around is not None:
        starts = range(round(around) - 30, round(around) + 31, START_STEP_DEG)
    elif map_up:
        base = georef.MAP_UP_TURNS[map_up] * 90
        starts = range(base - 30, base + 31, START_STEP_DEG)
    else:
        starts = range(0, 360, START_STEP_DEG)
    found: list[dict] = []
    for deg in starts:
        pre = georef.compose(georef.FLIP_Y, georef.similarity(scale or 1, deg, (0, 0), 0, 0))
        placed = georef.apply(outline, pre)
        t = georef.compose(pre, georef._bbox_align(placed, target) if scale is None else georef._center_align(placed, target))
        t, iou = georef.refine(outline, target, t, rounds=2, scale_lock=scale is not None)
        angle = _image_angle(t)
        same = next((f for f in found if _angle_gap(f["angle"], angle) < 20), None)
        if same is None:
            found.append({"angle": angle, "iou": iou, "t": t})
        elif iou > same["iou"]:
            same.update(angle=angle, iou=iou, t=t)
    return sorted(found, key=lambda f: -f["iou"])


def auto_fit(path, target: BaseGeometry, map_up: str | None = None,
             like_angle: float | None = None, tile_map: bool = False,
             north_cw: float | None = None, frame_angle: float = 0.0,
             outline: BaseGeometry | None = None, true_width_m: float | None = None) -> tuple[list[float], float, dict]:
    """Choose a placement: (transform from image pixels, IoU 0..1, notes).

    `like_angle` (the image-x direction of a floor already placed in this building) picks the candidate that
    turns the same way. Without it, if the best two candidates (more than 45 degrees apart) are within
    AMBIGUOUS_IOU of each other, `notes["ambiguous"]` lists them: the overlap cannot tell which way is up,
    so the caller should be given `map_up` (or another placed floor) rather than trust the pick.

    `north_cw` is the compass reading: degrees clockwise from the image's up to north (0 = north is up, 90 = it
    points right; read it off the drawing's north arrow). It is exactly the image-x angle of the placement, so the
    fit starts around it and only placements near it are considered (`notes["north_cw"]`; `notes["off_north_deg"]`
    is how far the overlap pulled the result away from it). `frame_angle` is how far the local frame is turned
    counter-clockwise from true east (`LocalFrame.angle`; the fit runs in the building's own axes).

    `outline` is the drawing's own outline in image pixels (y down), for a drawing whose background is not blank (a
    seating chart read from its zones, `main.py basemap --outline-trace`); it replaces the one read from the image.

    `true_width_m` is the real width, in metres, of the drawing's outline from left to right in the image (a dimension
    printed on the plan). The drawing is then placed at that scale (`notes["m_per_px"]`) and only turned and moved, so a
    footprint that is not the building's real size (OSM still holds the pre-construction building) cannot stretch it."""
    around = (north_cw - frame_angle) % 360 if north_cw is not None else None
    if true_width_m is not None:
        if true_width_m <= 0:
            raise ValueError("true_width_m must be positive")
        shape = outline if outline is not None else (tile_map_outline(path)[0] if tile_map
                                                     else image_outline(path, max_side=800, page_colour=True)[0])
        x0, _, x1, _ = shape.bounds
        m_per_px = true_width_m / (x1 - x0)
        cands = fit_outline(shape, target, map_up, around, scale=m_per_px)
    else:
        m_per_px = None
        cands = (fit_outline(outline, target, map_up, around) if outline is not None
                 else fit_outline(tile_map_outline(path)[0], target, map_up, around) if tile_map
                 else fit_orientations(path, target, map_up, around))
    best = cands[0]
    notes: dict = {"candidates": [{"angle": round(c["angle"]), "iou": round(c["iou"], 3)} for c in cands[:4]]}
    if m_per_px is not None:
        notes.update(true_width_m=true_width_m, m_per_px=round(m_per_px, 5))
    if around is not None:
        notes["north_cw"] = round(north_cw % 360, 1)
        notes["off_north_deg"] = round(_angle_gap(_image_angle(best["t"]), around), 1)
    elif like_angle is not None:
        best = min((c for c in cands if c["iou"] > cands[0]["iou"] - 0.2) or cands,
                   key=lambda c: _angle_gap(c["angle"], like_angle))
        notes["like_angle"] = round(like_angle)
    else:
        rival = next((c for c in cands[1:] if _angle_gap(c["angle"], best["angle"]) > 45), None)
        if rival and best["iou"] - rival["iou"] < AMBIGUOUS_IOU:
            notes["ambiguous"] = True
    return best["t"], best["iou"], notes
