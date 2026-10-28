"""Do the floors of a building stand on top of each other? A check on the lifts, stairs and escalators.

An elevator shaft, a staircase and (a little less strictly) an escalator run through several floors, so their
pictograms sit at nearly the same place on each floor. When every floor map was placed on its own (by the outline,
by a few gates), a floor that is shifted shows up as its shafts being all off by the same offset from the next floor's.

`report(fc)` compares each floor with the next one that has shafts: how many shafts agree with no offset, and which
single offset would make the most of them agree (a vote over the same-icon pairs). A pair with fewer than
MIN_SHAFTS shafts on a side cannot tell: it says so instead of guessing. It only reports; moving a floor is left to
`nudge-map` after looking at the preview.
"""

from __future__ import annotations

import math

from shapely.geometry import shape

from indoor_gen.geo_utils import LocalFrame

SHAFT_ICONS = ("elevator", "stairs", "escalator")
TOLERANCE_M = 3.0     # shafts closer than this (after the offset) count as the same shaft
MAX_OFFSET_M = 30.0   # a floor shifted by more than this is not a placement error but another building part
MIN_SHAFTS = 2        # fewer shafts on either floor: no verdict


def shafts_by_level(fc: dict) -> dict[int, list[tuple[str, float, float]]]:
    """{level: [(icon, x, y)]} of a built feature collection, in the building's metric frame."""
    md = fc.get("metadata") or {}
    frame = LocalFrame(shape(md["footprint"]), md.get("crs", "auto"))
    out: dict[int, list] = {}
    for f in fc["features"]:
        p = f["properties"]
        if p.get("shape_type") == "poi" and p.get("icon") in SHAFT_ICONS:
            q = frame.to_local(shape(f["geometry"]))
            out.setdefault(p["level"], []).append((p["icon"], q.x, q.y))
    return out


def matches(a: list, b: list, dx: float = 0.0, dy: float = 0.0, tol: float = TOLERANCE_M) -> int:
    """One-to-one count of same-icon shafts of `a` (shifted by dx, dy) within `tol` of a shaft of `b`."""
    pairs = sorted((math.hypot(xb - (xa + dx), yb - (ya + dy)), i, j)
                   for i, (ia, xa, ya) in enumerate(a) for j, (ib, xb, yb) in enumerate(b) if ia == ib)
    used_a, used_b, n = set(), set(), 0
    for d, i, j in pairs:
        if d > tol:
            break
        if i not in used_a and j not in used_b:
            used_a.add(i)
            used_b.add(j)
            n += 1
    return n


def best_offset(a: list, b: list, tol: float = TOLERANCE_M, max_offset: float = MAX_OFFSET_M) -> tuple[int, float, float]:
    """(matches, dx, dy): the shift of `a` that makes the most shafts agree with `b`, tried at every same-icon pair's
    difference. Ties go to the smaller shift."""
    cands = {(0.0, 0.0)}
    for ia, xa, ya in a:
        for ib, xb, yb in b:
            if ia == ib and math.hypot(xb - xa, yb - ya) <= max_offset:
                cands.add((xb - xa, yb - ya))
    scored = [(matches(a, b, dx, dy, tol), -math.hypot(dx, dy), dx, dy) for dx, dy in cands]
    n, _, dx, dy = max(scored)
    return n, dx, dy


def report(fc: dict, tol: float = TOLERANCE_M) -> list[dict]:
    """One row per pair of neighbouring floors that have shafts: counts, the best offset and a verdict."""
    by = shafts_by_level(fc)
    levels = sorted(by)
    names = {}
    for f in fc["features"]:
        p = f["properties"]
        if p.get("type") == "floor":
            names[p["level"]] = p.get("description") or p.get("name")
    rows = []
    for lo, hi in zip(levels, levels[1:]):
        a, b = by[lo], by[hi]
        row = {"floors": [names.get(lo, str(lo)), names.get(hi, str(hi))], "shafts": [len(a), len(b)],
               "agree_now": matches(a, b, tol=tol)}
        if min(len(a), len(b)) < MIN_SHAFTS:
            row["verdict"] = "too few shafts to tell"
        else:
            n, dx, dy = best_offset(a, b, tol)
            row.update(best_matches=n, offset_m=round(math.hypot(dx, dy), 1))
            if n <= row["agree_now"] or math.hypot(dx, dy) <= tol:
                row["verdict"] = "stacked" if row["agree_now"] >= MIN_SHAFTS else "too few shafts agree to tell"
            else:
                row["verdict"] = f"upper floor {math.hypot(dx, dy):.1f} m off ({n} shafts agree after the shift)"
        rows.append(row)
    return rows


def format_report(rows: list[dict]) -> str:
    if not rows:
        return "stacking: no floor has lifts, stairs or escalators to compare"
    lines = ["stacking (lifts / stairs / escalators on neighbouring floors):"]
    for r in rows:
        lines.append(f"  {r['floors'][0]:>4} -> {r['floors'][1]:<4} shafts {r['shafts'][0]:>2} / {r['shafts'][1]:<2} "
                     f"agree now {r['agree_now']:>2}  {r['verdict']}")
    return "\n".join(lines)
