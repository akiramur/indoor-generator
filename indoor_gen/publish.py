"""Publish every built building as ONE Mapbox tileset (Mapbox Tiling Service).

output/<slug>/indoor.geojson (all buildings) -> one line-delimited GeoJSON -> tileset source -> recipe -> publish.
Each feature carries `building` (slug) and `building_name`, so one style layer serves every building
(filter on `building` / `level`). The style itself is never touched: `style_snippet` only prints what to add.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import requests

API = "https://api.mapbox.com"
LAYER = "indoor"  # source-layer name in the tileset
DEFAULTS = {"username": "", "default_target": "dev", "targets": {"dev": "indoor-dev", "prod": "indoor"},
            "minzoom": 14, "maxzoom": 16}


class PublishError(Exception):
    pass


def load_settings(root: Path) -> dict:
    """publish.json at the repo root, e.g. {"username": "YOUR_MAPBOX_USERNAME", "tileset": "indoor"}."""
    path = root / "publish.json"
    return {**DEFAULTS, **(json.loads(path.read_text(encoding="utf-8")) if path.exists() else {})}


def resolve_target(cfg: dict, target: str | None = None, tileset: str | None = None) -> tuple[str, str]:
    """(target name, tileset name). Order: an explicit tileset name, --target, default_target.
    The default is dev: production has to be asked for by name."""
    if tileset:
        return "custom", tileset
    name = target or cfg["default_target"]
    if name not in cfg["targets"]:
        raise PublishError(f"unknown target {name!r}; publish.json has: {', '.join(cfg['targets'])}")
    return name, cfg["targets"][name]


def collect(output_root: Path, root: Path, exclude: set[str] = frozenset(), only: set[str] | None = None):
    """(slug, feature collection) per built building. Hand-written samples (config under samples/) are skipped
    unless named in `only`."""
    found, skipped = [], []
    for geo in sorted(output_root.glob("*/indoor.geojson")):
        slug = geo.parent.name
        if slug in exclude or (only is not None and slug not in only):
            continue
        if only is None and _is_sample(geo.parent, root):
            skipped.append(slug)
            continue
        found.append((slug, json.loads(geo.read_text(encoding="utf-8"))))
    return found, skipped


def _is_sample(out_dir: Path, root: Path) -> bool:
    info = out_dir / "build_info.json"
    if not info.exists():
        return False
    try:
        cfg = Path(json.loads(info.read_text(encoding="utf-8")).get("config_path", "")).resolve()
    except (OSError, ValueError):
        return False
    return (root / "samples").resolve() in cfg.parents


def to_ndjson(buildings) -> tuple[str, dict]:
    """Line-delimited GeoJSON of all features, tagged with their building, plus a per-building count."""
    lines, counts = [], {}
    for slug, fc in buildings:
        name = fc.get("metadata", {}).get("building", slug)
        for f in fc["features"]:
            props = {k: v for k, v in f["properties"].items() if v is not None}
            props.update(building=slug, building_name=name)  # handy for filtering; the structure id says it too
            lines.append(json.dumps({"type": "Feature", "properties": props, "geometry": f["geometry"]},
                                    ensure_ascii=False, separators=(",", ":")))
        counts[slug] = len(fc["features"])
    return "\n".join(lines) + "\n", counts


def structure_ids(buildings) -> list[str]:
    return [f["properties"]["id"] for _, fc in buildings for f in fc["features"]
            if f["properties"].get("type") == "structure"]


def recipe(username: str, source_id: str, minzoom: int, maxzoom: int) -> dict:
    return {"version": 1, "layers": {LAYER: {"source": f"mapbox://tileset-source/{username}/{source_id}",
                                             "minzoom": minzoom, "maxzoom": maxzoom}}}


# Badge per `icon` of the point POIs (elevator, stairs, toilets, ...): label, colour. A style needs the images itself,
# so the snippet carries this table and the viewer / preview draw `poi-<icon>` badges from it.
POI_BADGES = {
    "toilet": ["WC", "#2f6fb3"], "toilet-male": ["♂", "#2f6fb3"], "toilet-female": ["♀", "#c0457a"],
    "elevator": ["EV", "#5b6b7a"], "escalator": ["ES", "#5b6b7a"], "stairs": ["ST", "#5b6b7a"], "atm": ["ATM", "#3a8f5c"],
    "shop": ["S", "#d9822b"], "restaurant": ["R", "#c8553d"], "cafe": ["C", "#8a5a3c"], "bank": ["¥", "#3a8f5c"],
    "hospital": ["+", "#d6336c"], "pharmacy": ["Rx", "#2b9a8f"],
    "seat": ["", "#2f6fb3"],   # our own: the vocabulary has no seat icon; drawn as a small rectangle, to scale
    "seat-2": ["", "#d98a1f"], "seat-3": ["", "#2f9e5b"],   # 2 / 3階席, coloured like the seating chart
    "seat-temp": ["", "#c9ccd0"],   # a seat that is not permanent (`fixed: false`, a wheelchair space): the same, in grey
}


def style_snippet(username: str, tileset: str, structure_ids: list[str] = ()) -> dict:
    """What to add to a style by hand: a source, the `indoor` block and the layers in the order the guide
    (docs.mapbox.com/help/dive-deeper/indoor-mapping) gives. Floors are switched by is-active-floor / IndoorControl."""
    active = ["is-active-floor", ["get", "floor_id"]]
    room = ["all", ["==", ["get", "type"], "room"], ["!=", ["get", "class"], "wall"], ["!=", ["get", "shape_type"], "poi"], active]
    poi = ["all", ["==", ["get", "type"], "room"], ["==", ["get", "shape_type"], "poi"], active]
    base = {"source": "indoor", "source-layer": LAYER}
    return {
        "source": {"indoor": {"type": "vector", "url": f"mapbox://{username}.{tileset}"}},
        "indoor": {sid: {"sourceId": "indoor", "sourceLayers": [LAYER]} for sid in structure_ids},
        "layers": [
            {"id": "building-clip", "type": "clip", **base, "filter": ["==", ["get", "type"], "facility"],
             "layout": {"clip-layer-types": ["model", "symbol"]}},
            {"id": "indoor-floor-slab", "type": "fill-extrusion", **base,
             "filter": ["all", ["==", ["get", "type"], "floor"], ["is-active-floor", ["get", "id"]],
                        [">", ["get", "ground_z_offset"], 0]],  # below ground there is nothing to stand on (and it flickers)
             "paint": {"fill-extrusion-color": "#cfcfcf", "fill-extrusion-base": ["get", "base_height"],
                       "fill-extrusion-height": ["-", ["get", "height"], 0.3],  # under the rooms: equal heights z-fight
                       "fill-extrusion-cast-shadows": False}},  # a floating floor would throw a big shadow over the map
            {"id": "indoor-room-fills", "type": "fill", **base, "filter": room,
             "paint": {"fill-color": ["get", "color"], "fill-opacity": 0.9, "fill-z-offset": ["get", "lift"]}},
            {"id": "indoor-walls", "type": "fill-extrusion", **base,
             "filter": ["all", ["==", ["get", "type"], "room"], ["==", ["get", "class"], "wall"], active],
             "paint": {"fill-extrusion-color": "#aaaaaa", "fill-extrusion-base": ["get", "lift"],
                       "fill-extrusion-height": ["+", ["get", "lift"], ["get", "wall_height"]],
                       "fill-extrusion-line-width": 0.3, "fill-extrusion-opacity": 0.8,
                       "fill-extrusion-cast-shadows": False}},
            {"id": "indoor-room-labels", "type": "symbol", **base, "filter": room,
             "layout": {"text-field": ["get", "name"], "text-size": 11, "symbol-z-elevate": True}},
            {"id": "indoor-poi", "type": "symbol", **base, "filter": poi,
             "layout": {"icon-image": ["concat", "poi-", ["get", "icon"]], "icon-size": 0.5, "icon-allow-overlap": True,
                        "symbol-z-elevate": True}},
        ],
        "poi_badges": POI_BADGES,
        "control": "map.addControl(new mapboxgl.IndoorControl(), 'bottom-right')  // GL JS >= 3.4; indoor shows at zoom >= 16",
        "note": "the clip layer must be first; the style needs Standard imported (showIndoor: false); "
                "add the `indoor` block to the style root; indoor-poi needs the `poi-<icon>` images "
                "(see poi_badges: a circle with the label, 56px, pixelRatio 2) added with map.addImage",
    }


def viewer_html(template: str, username: str, tileset: str, buildings, token: str | None) -> str:
    """A standalone page that shows the published tileset the way the guide describes (check page, not the preview)."""
    structures = [{"id": f["properties"]["id"], "name": f["properties"]["name"],
                   "lon": f["properties"]["center_lon"], "lat": f["properties"]["center_lat"]}
                  for _, fc in buildings for f in fc["features"] if f["properties"].get("type") == "structure"]
    snippet = style_snippet(username, tileset, [s["id"] for s in structures])
    page = template.replace("__TILESET_ID_JSON__", json.dumps(f"{username}.{tileset}"))
    for key, val in (("__TILESET_ID__", f"{username}.{tileset}"), ("__TILESET__", tileset)):
        page = page.replace(key, val)
    # `</` is escaped so a name cannot close the script element
    dump = lambda o: json.dumps(o, ensure_ascii=False).replace("</", "<\\/")
    return (page.replace("__STRUCTURES__", dump(structures)).replace("__SNIPPET__", dump(snippet))
            .replace("__TOKEN__", dump(token)))


class Mts:
    """Mapbox Tiling Service calls for one account. The secret token goes in the query string (the API's
    contract) and never into output or error text."""

    def __init__(self, username: str, token: str):
        self.username, self.token = username, token

    def _call(self, method: str, path: str, **kw) -> requests.Response:
        try:
            r = requests.request(method, f"{API}{path}", params={"access_token": self.token}, timeout=300, **kw)
        except requests.RequestException as e:
            raise PublishError(f"{method} {path}: {type(e).__name__}") from None
        return r

    @staticmethod
    def _check(r: requests.Response, what: str) -> dict:
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", r.text)
            except ValueError:
                msg = r.text
            raise PublishError(f"{what}: HTTP {r.status_code} {msg[:300]}")
        try:
            return r.json()
        except ValueError:
            return {}

    def put_source(self, source_id: str, ndjson: str) -> dict:
        path = f"/tilesets/v1/sources/{self.username}/{source_id}"
        # PUT replaces an existing source; POST creates (and would append to an existing one).
        exists = self._call("GET", path).status_code == 200
        r = self._call("PUT" if exists else "POST", path,
                       files={"file": ("indoor.ndjson", ndjson.encode("utf-8"), "application/octet-stream")})
        return self._check(r, "upload tileset source")

    def upsert_tileset(self, tileset_id: str, rec: dict, name: str) -> str:
        r = self._call("GET", f"/tilesets/v1/{tileset_id}/recipe")
        if r.status_code == 200:
            self._check(self._call("PATCH", f"/tilesets/v1/{tileset_id}/recipe", json=rec), "update recipe")
            return "updated"
        if r.status_code not in (404,):
            self._check(r, "read recipe")
        self._check(self._call("POST", f"/tilesets/v1/{tileset_id}", json={"recipe": rec, "name": name}),
                    "create tileset")
        return "created"

    def publish(self, tileset_id: str) -> str:
        return self._check(self._call("POST", f"/tilesets/v1/{tileset_id}/publish"), "publish")["jobId"]

    def wait(self, tileset_id: str, job_id: str, timeout: float = 900, every: float = 5) -> dict:
        deadline = time.time() + timeout
        while True:
            job = self._check(self._call("GET", f"/tilesets/v1/{tileset_id}/jobs/{job_id}"), "job status")
            if job.get("stage") in ("success", "failed"):
                return job
            if time.time() > deadline:
                raise PublishError(f"job {job_id} still {job.get('stage')} after {timeout:.0f}s; "
                                   f"check it in Studio > Tilesets")
            time.sleep(every)
