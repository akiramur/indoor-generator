"""Coordinate helpers: WGS84 <-> metric local frame aligned to the building."""

from __future__ import annotations

import math

from pyproj import CRS, Transformer
from shapely import affinity
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform


def utm_crs_for(lon: float, lat: float) -> CRS:
    zone = int((lon + 180) // 6) + 1
    return CRS.from_epsg((32600 if lat >= 0 else 32700) + zone)


def main_axis_angle(geom: BaseGeometry) -> float:
    """Angle (deg) of the building's long axis, folded to [-45, 45).

    Folding means the cut directions stay closest to true compass
    directions, so "west" is the side of the building facing roughly west.
    """
    rect = geom.minimum_rotated_rectangle
    coords = list(rect.exterior.coords)
    edges = [(coords[i], coords[i + 1]) for i in range(4)]
    (x0, y0), (x1, y1) = max(edges, key=lambda e: math.dist(*e))
    angle = math.degrees(math.atan2(y1 - y0, x1 - x0))
    return (angle + 45) % 90 - 45


class LocalFrame:
    """Projects WGS84 geometries into a metric frame rotated so the
    building's main axes are parallel to x (east) and y (north)."""

    def __init__(self, footprint_wgs84: BaseGeometry, crs: str = "auto", orientation="auto"):
        c = footprint_wgs84.centroid
        self.crs = utm_crs_for(c.x, c.y) if crs in (None, "auto") else CRS.from_user_input(crs)
        self._fwd = Transformer.from_crs("EPSG:4326", self.crs, always_xy=True)
        self._inv = Transformer.from_crs(self.crs, "EPSG:4326", always_xy=True)
        projected = transform(self._fwd.transform, footprint_wgs84)
        self.origin = projected.centroid
        if orientation == "auto":
            self.angle = main_axis_angle(projected)
        elif orientation == "north":
            self.angle = 0.0
        else:
            self.angle = float(orientation)

    def to_local(self, geom: BaseGeometry) -> BaseGeometry:
        g = transform(self._fwd.transform, geom)
        return affinity.rotate(g, -self.angle, origin=self.origin, use_radians=False)

    def to_wgs84(self, geom: BaseGeometry) -> BaseGeometry:
        g = affinity.rotate(geom, self.angle, origin=self.origin, use_radians=False)
        return transform(self._inv.transform, g)
