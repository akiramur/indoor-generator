"""Helpers shared by the floor-guide importers and `main.py` (`_write_plans`)."""

from __future__ import annotations

from shapely.geometry.base import BaseGeometry

LOW_FIT = 0.7  # map outline vs footprint IoU below which an importer warns


def floor_level(name: str) -> int:
    name = name.upper().rstrip("F")
    return -int(name[1:]) if name.startswith("B") else int(name)


def same_framing(a: BaseGeometry, b: BaseGeometry, tolerance: float = 0.03) -> bool:
    """True when two map outlines occupy the same box on the canvas."""
    ab, bb = a.bounds, b.bounds
    size = max(bb[2] - bb[0], bb[3] - bb[1])
    return all(abs(x - y) <= tolerance * size for x, y in zip(ab, bb))


def share_frames(analyses: list[dict]) -> None:
    """Use the best map->footprint fit for every floor drawn in the same framing.

    Floor maps of one building are usually drawn at the same scale and
    position, but a single floor's outline can match the footprint badly
    (setbacks, terraces). The best-fitting floor is the most reliable.
    """
    if not analyses:
        return
    ref = max(analyses, key=lambda a: a["fit2"])
    for a in analyses:
        if a is ref:
            continue
        if same_framing(a["outline"], ref["outline"]):
            a["t_svg_local"] = ref["t_svg_local"]
            a["report"]["map_frame_from"] = ref["floor"]["name"]
        elif a["fit2"] < LOW_FIT:
            a["report"]["warnings"].append(
                f"map outline matches the footprint poorly (IoU {a['fit2']:.2f}); check map_up")


def debug_svg(footprint_local: BaseGeometry, plan_local: list[tuple[str, BaseGeometry]]) -> str:
    """A quick north-up drawing (footprint + imported tenants) for checking a fit."""
    geoms = [footprint_local] + [g for _, g in plan_local]
    minx = min(g.bounds[0] for g in geoms) - 5
    maxy = max(g.bounds[3] for g in geoms) + 5
    w = max(g.bounds[2] for g in geoms) + 5 - minx
    h = maxy - (min(g.bounds[1] for g in geoms) - 5)

    def d(g):
        return "".join("M" + " L".join(f"{x - minx:.1f},{maxy - y:.1f}" for x, y in p.exterior.coords) + "Z"
                       for p in getattr(g, "geoms", [g]))
    parts = [f'<path d="{d(footprint_local)}" fill="#eee" stroke="#333"/>']
    for label, g in plan_local:
        c = g.centroid
        parts.append(f'<path d="{d(g)}" fill="#f6a" fill-opacity=".5" stroke="#a03" stroke-width=".4"/>'
                     f'<text x="{c.x - minx:.1f}" y="{maxy - c.y:.1f}" font-size="3" text-anchor="middle">{label}</text>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.0f} {h:.0f}" width="900">'
            f'<text x="2" y="5" font-size="4">N ↑</text>{"".join(parts)}</svg>')
