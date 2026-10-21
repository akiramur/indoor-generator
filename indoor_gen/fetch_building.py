"""Fetch building footprints from OpenStreetMap (Nominatim + Overpass)."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import requests
from pyproj import Geod
from shapely.geometry import LineString, Point, Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize, unary_union

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSM_API_URL = "https://api.openstreetmap.org/api/0.6"
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
USER_AGENT = "indoor-generator/0.1 (+https://github.com/akiramur/indoor-generator)"
DEFAULT_RADIUS_M = 60
# `body` keeps relation members (the `tags` verbosity drops them, leaving relations without geometry).
OUT_GEOM = "out body geom;"
_GEOD = Geod(ellps="WGS84")
_last_nominatim_call = 0.0


class FetchError(Exception):
    pass


def geodesic_area(geom: BaseGeometry) -> float:
    return abs(_GEOD.geometry_area_perimeter(geom)[0])


def nominatim_search(query: str, limit: int = 5) -> list[dict]:
    global _last_nominatim_call
    wait = 1.0 - (time.time() - _last_nominatim_call)
    if wait > 0:  # Nominatim usage policy: max 1 request/second
        time.sleep(wait)
    _last_nominatim_call = time.time()
    resp = requests.get(
        NOMINATIM_URL,
        params={"q": query, "format": "jsonv2", "limit": limit, "accept-language": "ja,en"},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def overpass(query: str, attempts: int = 3) -> dict:
    """Run an Overpass query, retrying overloaded servers (429/504) with backoff."""
    last_err = None
    for attempt in range(attempts):
        for url in OVERPASS_URLS:
            try:
                resp = requests.post(url, data={"data": query},
                                     headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=90)
                if resp.status_code == 200:
                    return resp.json()
                last_err = f"{url}: HTTP {resp.status_code}"
            except requests.RequestException as e:
                last_err = f"{url}: {e}"
        if attempt < attempts - 1:
            time.sleep(5 * (attempt + 1))
    raise FetchError(f"all Overpass endpoints failed ({last_err})")


def _way_coords(geometry: list[dict]) -> list[tuple[float, float]]:
    return [(p["lon"], p["lat"]) for p in geometry]


def element_to_geometry(el: dict) -> BaseGeometry | None:
    """Build a (Multi)Polygon from an Overpass element fetched with `out geom`."""
    if el["type"] == "way":
        coords = _way_coords(el.get("geometry", []))
        if len(coords) < 4 or coords[0] != coords[-1]:
            return None
        poly = Polygon(coords)
        return poly if poly.is_valid else poly.buffer(0)
    if el["type"] == "relation":
        outers, inners = [], []
        for m in el.get("members", []):
            if m.get("type") != "way" or "geometry" not in m:
                continue
            line = LineString(_way_coords(m["geometry"]))
            (inners if m.get("role") == "inner" else outers).append(line)
        outer = unary_union(list(polygonize(unary_union(outers)))) if outers else None
        if outer is None or outer.is_empty:
            return None
        if inners:
            inner = unary_union(list(polygonize(unary_union(inners))))
            outer = outer.difference(inner)
        return outer
    return None


def _candidate(el: dict, geom: BaseGeometry, point: Point | None) -> dict:
    tags = el.get("tags", {})
    cand = {
        "osm_type": el["type"],
        "osm_id": el["id"],
        "name": tags.get("name:ja") or tags.get("name") or tags.get("addr:housename"),
        "building": tags.get("building"),
        "levels": tags.get("building:levels"),
        "min_level": tags.get("building:min_level"),
        "underground_levels": tags.get("building:levels:underground"),
        "height": tags.get("height"),
        "area_m2": round(geodesic_area(geom), 1),
        "center": [round(geom.centroid.x, 7), round(geom.centroid.y, 7)],
        "tags": tags,
    }
    if point is not None:
        cand["contains_point"] = geom.contains(point)
        cand["distance_m"] = round(_GEOD.inv(point.x, point.y, *geom.centroid.coords[0])[2], 1)
    return cand


def buildings_near(lat: float, lon: float, radius_m: float = DEFAULT_RADIUS_M) -> list[tuple[dict, BaseGeometry]]:
    q = f"""[out:json][timeout:60];
(way["building"](around:{radius_m},{lat},{lon});
 relation["building"](around:{radius_m},{lat},{lon}););
{OUT_GEOM}"""
    point = Point(lon, lat)
    out = []
    for el in overpass(q).get("elements", []):
        geom = element_to_geometry(el)
        if geom is not None and not geom.is_empty:
            out.append((_candidate(el, geom, point), geom))
    # Buildings containing the point first, then by distance.
    out.sort(key=lambda cg: (not cg[0]["contains_point"], cg[0]["distance_m"]))
    return out


def _osm_api_element(osm_type: str, osm_id: int) -> dict:
    """A way or relation in Overpass `out geom` shape, from the main OSM API."""
    resp = requests.get(f"{OSM_API_URL}/{osm_type}/{osm_id}/full.json", headers={"User-Agent": USER_AGENT}, timeout=60)
    resp.raise_for_status()
    return osm_api_to_overpass(resp.json()["elements"], osm_type, osm_id)


def osm_api_to_overpass(elements: list[dict], osm_type: str, osm_id: int) -> dict:
    nodes = {e["id"]: {"lon": e["lon"], "lat": e["lat"]} for e in elements if e["type"] == "node"}
    ways = {e["id"]: e for e in elements if e["type"] == "way"}
    if osm_type == "way":
        way = ways[osm_id]
        return {"type": "way", "id": osm_id, "tags": way.get("tags", {}), "geometry": [nodes[n] for n in way["nodes"]]}
    rel = next(e for e in elements if e["type"] == "relation" and e["id"] == osm_id)
    members = [{"type": "way", "ref": m["ref"], "role": m.get("role", ""),
                "geometry": [nodes[n] for n in ways[m["ref"]]["nodes"]]}
               for m in rel.get("members", []) if m["type"] == "way" and m["ref"] in ways]
    return {"type": "relation", "id": osm_id, "tags": rel.get("tags", {}), "members": members}


def building_by_id(osm_type: str, osm_id: int) -> tuple[dict, BaseGeometry]:
    # The main OSM API answers a single element quickly; Overpass is often slow or overloaded.
    try:
        elements = [_osm_api_element(osm_type, osm_id)]
    except (requests.RequestException, KeyError, StopIteration) as api_err:
        try:
            elements = overpass(f"[out:json][timeout:60];{osm_type}({osm_id});{OUT_GEOM}").get("elements", [])
        except FetchError as e:
            raise FetchError(f"the OSM API and Overpass both failed for {osm_type}/{osm_id}: "
                             f"{api_err!r}; {e}") from e
    for el in elements:
        geom = element_to_geometry(el)
        if geom is not None and not geom.is_empty:
            return _candidate(el, geom, None), geom
    raise FetchError(f"{osm_type}/{osm_id} has no polygon geometry")


def building_parts(footprint: BaseGeometry, min_inside: float = 0.9) -> list[tuple[dict, BaseGeometry]]:
    """OSM `building:part` shapes lying inside a footprint (towers, podiums, setbacks).

    Their height / min_height / building:levels / building:min_level tags tell
    which floors each part covers.
    """
    c = footprint.centroid
    radius = max(_GEOD.inv(c.x, c.y, x, y)[2] for x, y in footprint.exterior.coords) + 10
    q = f"""[out:json][timeout:60];
(way["building:part"](around:{radius:.0f},{c.y},{c.x});
 relation["building:part"](around:{radius:.0f},{c.y},{c.x}););
{OUT_GEOM}"""
    parts = []
    for el in overpass(q).get("elements", []):
        geom = element_to_geometry(el)
        if geom is None or geom.is_empty or geom.intersection(footprint).area < min_inside * geom.area:
            continue
        tags = el.get("tags", {})
        info = {"osm_type": el["type"], "osm_id": el["id"], "area_m2": round(geodesic_area(geom), 1),
                "share_of_footprint": round(geom.area / footprint.area, 3),
                **{k: tags.get(k) for k in ("name", "height", "min_height", "building:levels", "building:min_level",
                                            "building:part") if tags.get(k) is not None}}
        parts.append((info, geom))
    parts.sort(key=lambda p: -p[1].area)
    return parts


def landmarks(footprint: BaseGeometry, margin_m: float = 150) -> list[dict]:
    """Named OSM points that floor maps label: entrances, ticket gates, station exits.

    Used to align a floor-map image automatically: the same names are found on the image,
    and these give their real positions. Sorted by distance from the footprint.
    """
    c = footprint.centroid
    hull = footprint.convex_hull
    radius = max(_GEOD.inv(c.x, c.y, x, y)[2] for x, y in hull.exterior.coords) + margin_m
    around = f"(around:{radius:.0f},{c.y},{c.x})"
    q = f"""[out:json][timeout:60];
(node["name"]["entrance"]{around};
 node["name"]["railway"~"^(train_station_entrance|subway_entrance)$"]{around};
 node["name"]["barrier"~"^(turnstile|gate)$"]{around};
 node["name"]["indoor"]{around};);
out;"""
    from shapely.ops import nearest_points

    out = []
    for el in overpass(q).get("elements", []):
        tags = el.get("tags", {})
        p = Point(el["lon"], el["lat"])
        q = nearest_points(footprint.boundary, p)[0]
        dist = 0.0 if footprint.contains(p) else _GEOD.inv(p.x, p.y, q.x, q.y)[2]
        kind = next((f"{k}={tags[k]}" for k in ("barrier", "railway", "entrance", "indoor") if k in tags), "")
        out.append({"osm_id": el["id"], "name": tags["name"], "kind": kind, "level": tags.get("level"),
                    "lnglat": [round(p.x, 7), round(p.y, 7)],
                    "outside_footprint_m": round(dist, 1)})
    out.sort(key=lambda d: d["outside_footprint_m"])
    return out


def is_building_category(category: str) -> bool:
    """Nominatim `category=type` of a building, e.g. `building=yes` or `historic=building` (heritage buildings)."""
    return "building" in category.split("=")


def locate(query: str | None = None, lat: float | None = None, lon: float | None = None,
           radius_m: float = DEFAULT_RADIUS_M, limit: int = 8) -> dict:
    """Return candidate buildings for a name/address or a coordinate."""
    places = []
    if query:
        for r in nominatim_search(query):
            places.append({
                "display_name": r.get("display_name"),
                "osm_type": r.get("osm_type"), "osm_id": int(r["osm_id"]) if r.get("osm_id") else None,
                "category": f"{r.get('category')}={r.get('type')}",
                "lat": float(r["lat"]), "lon": float(r["lon"]),
            })
        if lat is None and places:
            lat, lon = places[0]["lat"], places[0]["lon"]
    if lat is None or lon is None:
        raise FetchError(f"no place found for {query!r}")

    candidates = [c for c, _ in buildings_near(lat, lon, radius_m)]
    # A Nominatim hit that is itself a building is the strongest candidate.
    for p in places[:1]:
        if p["osm_type"] in ("way", "relation") and is_building_category(p["category"]):
            match = next((c for c in candidates
                          if c["osm_type"] == p["osm_type"] and c["osm_id"] == p["osm_id"]), None)
            if match is None:  # e.g. a large building whose centroid is outside the search radius
                try:
                    match, _ = building_by_id(p["osm_type"], p["osm_id"])
                    candidates.append(match)
                except FetchError:
                    pass
            if match is not None:
                match["matches_search"] = True
            candidates.sort(key=lambda c: not c.get("matches_search", False))
    candidates = candidates[:limit]
    for c in candidates:
        c.pop("tags", None)
    return {"query": query, "point": {"lat": lat, "lon": lon}, "places": places, "candidates": candidates}


def _source_key(source: dict) -> str:
    return hashlib.sha1(json.dumps(source, sort_keys=True).encode()).hexdigest()[:12]


def resolve_footprint(source: dict, cache_path: Path, refresh: bool = False,
                      base_dir: Path | None = None) -> tuple[BaseGeometry, dict]:
    """Get the footprint for a config `building.source`, using a local cache."""
    key = _source_key(source)
    # A GeoJSON file is read every time: the key only holds its path, so a cached copy would hide an edited file.
    if cache_path.exists() and not refresh and source.get("type") != "geojson":
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("properties", {}).get("source_key") == key:
            return shape(cached["geometry"]), cached["properties"]

    t = source["type"]
    if t == "geojson":
        p = Path(source["path"])
        if not p.is_absolute() and base_dir is not None:
            p = base_dir / p
        obj = json.loads(p.read_text(encoding="utf-8"))
        if obj.get("type") == "FeatureCollection":
            obj = obj["features"][0]
        geom = shape(obj["geometry"] if obj.get("type") == "Feature" else obj)
        info = {"osm_type": None, "osm_id": None, "name": None, "area_m2": round(geodesic_area(geom), 1)}
    elif t == "osm_id":
        info, geom = building_by_id(source["osm_type"], source["osm_id"])
    else:
        lat, lon = source.get("lat"), source.get("lon")
        if t == "query" and (lat is None or lon is None):
            hits = nominatim_search(source["query"], limit=1)
            if not hits:
                raise FetchError(f"Nominatim found nothing for {source['query']!r}")
            lat, lon = float(hits[0]["lat"]), float(hits[0]["lon"])
            if hits[0].get("osm_type") in ("way", "relation") and is_building_category(f"{hits[0].get('category')}={hits[0].get('type')}"):
                try:
                    info, geom = building_by_id(hits[0]["osm_type"], int(hits[0]["osm_id"]))
                    lat = None
                except FetchError:
                    pass
        if lat is not None:
            found = buildings_near(lat, lon, source.get("search_radius_m", DEFAULT_RADIUS_M))
            if not found:
                raise FetchError(f"no OSM building within {source.get('search_radius_m', DEFAULT_RADIUS_M)} m of {lat},{lon}")
            info, geom = found[0]

    info = {k: v for k, v in info.items() if k != "tags"}
    info["source_key"] = key
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"type": "Feature", "properties": info, "geometry": mapping(geom)},
                                     ensure_ascii=False), encoding="utf-8")
    return geom, info
