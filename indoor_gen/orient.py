"""Which way is a floor map turned? Score the four quarter turns of its placement by how well the names printed on the map
(the traced shops) land on OSM features of the same name (`fetch_building.named_places`).

The map is only turned about its own centre: `align.auto_fit` / `basemap --map-up` already found a placement that covers the
footprint, but a long or near-symmetric building fits equally well turned by 90° or 180°, and the outline cannot tell. Names
can: a shop that is called the same on the map and in OSM must sit near its OSM point. A turn is `clear` when at least two
names agree, its median distance is under CLEAR_MAX_M and under half of the next best; otherwise `undecided` (too few
names, or OSM is as rough as the drawing), and the caller should read a compass or a landmark instead."""

from __future__ import annotations

import math
import re
import statistics
import unicodedata

CLEAR_MAX_M = 60.0
M_PER_DEG_LAT = 111_320.0


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKC", name).casefold()
    return re.sub(r"[\s\-・･.,'’&()（）「」]+", "", s)


def same_name(a: str, b: str) -> bool:
    a, b = norm(a), norm(b)
    return bool(a and b) and (a == b or (min(len(a), len(b)) >= 3 and (a in b or b in a)))


def px_to_lnglat(corners: list[list[float]], size: tuple[float, float], px: tuple[float, float]) -> tuple[float, float]:
    """Bilinear position of an image pixel; `corners` are the image's top-left, top-right, bottom-right, bottom-left."""
    u, v = px[0] / size[0], px[1] / size[1]
    tl, tr, br, bl = corners
    return tuple((1 - u) * (1 - v) * tl[i] + u * (1 - v) * tr[i] + u * v * br[i] + (1 - u) * v * bl[i] for i in (0, 1))


def turned(corners: list[list[float]], quarter_turns: int) -> list[list[float]]:
    """The placement turned clockwise by `quarter_turns` x 90° about the centre of its corners (the image moves, not the ground)."""
    cx = sum(c[0] for c in corners) / 4
    cy = sum(c[1] for c in corners) / 4
    k = math.cos(math.radians(cy))
    out = []
    for lon, lat in corners:
        x, y = (lon - cx) * k * M_PER_DEG_LAT, (lat - cy) * M_PER_DEG_LAT
        for _ in range(quarter_turns % 4):
            x, y = y, -x
        out.append([cx + x / (k * M_PER_DEG_LAT), cy + y / M_PER_DEG_LAT])
    return out


def dist_m(a, b) -> float:
    k = math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((a[0] - b[0]) * k, a[1] - b[1]) * M_PER_DEG_LAT


def zone_centres(trace: list[dict]) -> list[tuple[str, tuple[float, float]]]:
    """(name, centre pixel) of every named outline of a `*-trace.json` (placeholder names like "(名称なし)" and icons are left out)."""
    out = []
    for z in trace:
        px = z.get("px")
        if z.get("name") and not z["name"].startswith("(") and px and not z.get("icon"):
            out.append((z["name"], (sum(p[0] for p in px) / len(px), sum(p[1] for p in px) / len(px))))
    return out


def score_turns(corners, size, named_px, places) -> list[dict]:
    """One row per quarter turn (0 = as placed, then 90° clockwise steps): matched names, median / mean distance in metres."""
    rows = []
    for k in range(4):
        cs = turned(corners, k)
        dists = []
        for name, px in named_px:
            where = px_to_lnglat(cs, size, px)
            near = [dist_m(where, p["lnglat"]) for p in places if same_name(name, p["name"])]
            if near:
                dists.append(min(near))
        rows.append({"turn_deg": 90 * k, "matched": len(dists),
                     "median_m": round(statistics.median(dists), 1) if dists else None,
                     "mean_m": round(statistics.fmean(dists), 1) if dists else None})
    return rows


def verdict(rows: list[dict]) -> dict:
    """The best turn and whether it is clear. Fewer matches than the most-matched turn count against a turn."""
    ranked = sorted((r for r in rows if r["median_m"] is not None), key=lambda r: r["median_m"])
    if not ranked or ranked[0]["matched"] < 2:
        return {"best_turn_deg": None, "status": "undecided", "why": "fewer than two names match an OSM feature"}
    best = ranked[0]
    second = ranked[1]["median_m"] if len(ranked) > 1 else None
    clear = best["median_m"] <= CLEAR_MAX_M and (second is None or best["median_m"] * 2 <= second)
    return {"best_turn_deg": best["turn_deg"], "status": "clear" if clear else "undecided",
            "why": f"{best['matched']} names, median {best['median_m']} m" + (f" vs {second} m for the next" if second else "")}
