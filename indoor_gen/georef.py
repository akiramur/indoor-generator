"""Fit a floor-plan drawing onto a building footprint.

Transforms are shapely affine parameter lists [a, b, d, e, xoff, yoff]:
    x' = a*x + b*y + xoff
    y' = d*x + e*y + yoff

A fit searches an orientation (quarter turns, optionally mirrored), then
refines scale / rotation / translation to maximise the overlap (IoU) of the
source shape with the target shape.
"""

from __future__ import annotations

import math

from shapely import affinity
from shapely.geometry.base import BaseGeometry
from shapely.validation import make_valid

IDENTITY = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
# Image coordinates (y down) -> math coordinates (y up).
FLIP_Y = [1.0, 0.0, 0.0, -1.0, 0.0, 0.0]
# Quarter turns that put the drawing's "up" on the given compass direction
# (after FLIP_Y), in a local frame where +x = east and +y = north.
MAP_UP_TURNS = {"north": 0, "west": 1, "south": 2, "east": 3}


def compose(*ts: list[float]) -> list[float]:
    """Compose transforms; the first argument is applied first."""
    out = IDENTITY
    for t in ts:
        a, b, d, e, xo, yo = t
        A, B, D, E, XO, YO = out
        out = [a * A + b * D, a * B + b * E, d * A + e * D, d * B + e * E,
               a * XO + b * YO + xo, d * XO + e * YO + yo]
    return out


def orientation(quarter_turns: int, mirror: bool = False) -> list[float]:
    m = [-1.0, 0.0, 0.0, 1.0, 0.0, 0.0] if mirror else IDENTITY
    th = math.pi / 2 * (quarter_turns % 4)
    c, s = round(math.cos(th)), round(math.sin(th))
    return compose(m, [c, -s, s, c, 0.0, 0.0])


def similarity(scale: float, rotation_deg: float, origin: tuple[float, float], dx: float, dy: float) -> list[float]:
    th = math.radians(rotation_deg)
    c, s = math.cos(th) * scale, math.sin(th) * scale
    ox, oy = origin
    return [c, -s, s, c, ox - c * ox + s * oy + dx, oy - s * ox - c * oy + dy]


def apply(geom: BaseGeometry, t: list[float]) -> BaseGeometry:
    return affinity.affine_transform(geom, t)


def iou(a: BaseGeometry, b: BaseGeometry) -> float:
    if not a.is_valid:
        a = make_valid(a)
    u = a.union(b).area
    return a.intersection(b).area / u if u else 0.0


def _bbox_align(src: BaseGeometry, target: BaseGeometry) -> list[float]:
    a, b = src.bounds, target.bounds
    s = ((b[2] - b[0]) / (a[2] - a[0]) + (b[3] - b[1]) / (a[3] - a[1])) / 2
    acx, acy = (a[0] + a[2]) / 2, (a[1] + a[3]) / 2
    bcx, bcy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    return [s, 0.0, 0.0, s, bcx - s * acx, bcy - s * acy]


def _center_align(src: BaseGeometry, target: BaseGeometry) -> list[float]:
    """Move `src` (its scale kept) so that its bounding box is centred on the target's."""
    a, b = src.bounds, target.bounds
    return [1.0, 0.0, 0.0, 1.0, (b[0] + b[2]) / 2 - (a[0] + a[2]) / 2, (b[1] + b[3]) / 2 - (a[1] + a[3]) / 2]


def refine(src: BaseGeometry, target: BaseGeometry, t: list[float], rounds: int = 4,
           scale_lock: bool = False) -> tuple[list[float], float]:
    """Coordinate search on scale / rotation / translation after `t` (with `scale_lock`, only rotation and translation)."""
    size = math.sqrt(target.area) or 1.0
    best_t, best = t, iou(apply(src, t), target)
    origin = (target.centroid.x, target.centroid.y)
    step_xy, step_rot, step_s = size * 0.04, 2.0, 0.03
    for _ in range(rounds):
        improved = True
        while improved:
            improved = False
            candidates = [] if scale_lock else [similarity(ds, 0, origin, 0, 0) for ds in (1 - step_s, 1 + step_s)]
            candidates += [similarity(1, dr, origin, 0, 0) for dr in (-step_rot, step_rot)]
            candidates += [similarity(1, 0, origin, dx, dy)
                           for dx, dy in ((step_xy, 0), (-step_xy, 0), (0, step_xy), (0, -step_xy))]
            for c in candidates:
                nt = compose(best_t, c)
                v = iou(apply(src, nt), target)
                if v > best + 1e-6:
                    best_t, best, improved = nt, v, True
        step_xy, step_rot, step_s = step_xy / 3, step_rot / 3, step_s / 3
    return best_t, best


def fit(src: BaseGeometry, target: BaseGeometry, base: list[float] | None = None,
        orientations: list[tuple[int, bool]] | None = None) -> tuple[list[float], float]:
    """Best transform mapping `src` onto `target`.

    `base` is applied before the orientation (e.g. FLIP_Y for image
    coordinates). `orientations` limits the search; default is all 8.
    """
    base = base or IDENTITY
    orientations = orientations or [(k, m) for k in range(4) for m in (False, True)]
    best_t, best = None, -1.0
    for k, m in orientations:
        pre = compose(base, orientation(k, m))
        t = compose(pre, _bbox_align(apply(src, pre), target))
        t, v = refine(src, target, t, rounds=2 if len(orientations) > 1 else 4)
        if v > best:
            best_t, best = t, v
    if len(orientations) > 1:
        best_t, best = refine(src, target, best_t, rounds=4)
    return best_t, best
