#!/usr/bin/env python3
"""Indoor map generator CLI.

  python main.py locate "丸の内ビルディング"
  python main.py validate samples/building_config.json
  python main.py build samples/building_config.json
  python main.py serve marubiru-sample
  python main.py undo samples/building_config.json
  python main.py basemap samples/building_config.json --level 3 --image 3F.png
  python main.py align samples/building_config.json
  python main.py cost marubiru-sample
  python main.py publish [--upload]
  python main.py compare marubiru-sample /path/to/other-worktree/output/marubiru-sample
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import sys
from pathlib import Path

from indoor_gen import build_preview, config, generate_geojson, history, quality, usage_cost
from indoor_gen.fetch_building import FetchError, building_parts, locate, resolve_footprint

ROOT = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    """KEY=VALUE lines of .env into os.environ; variables already set in the shell win."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_dotenv(ROOT / ".env")
OUTPUT_ROOT = ROOT / "output"


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _out_dir(slug: str) -> Path:
    return OUTPUT_ROOT / slug


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def cmd_locate(args) -> int:
    try:
        result = locate(args.query, args.lat, args.lon, args.radius, args.limit)
    except FetchError as e:
        _print_json({"ok": False, "error": str(e)})
        return 1
    _print_json(result)
    return 0


def cmd_validate(args) -> int:
    path = Path(args.config)
    try:
        raw = config.load_raw(path)
    except config.ConfigError as e:
        _print_json({"ok": False, "errors": e.errors})
        return 1
    errors = config.validate_raw(raw, base_dir=path.resolve().parent)
    _print_json({"ok": not errors, "slug": config.slug_for(path, raw), "errors": errors})
    return 1 if errors else 0


def summarise(fc: dict, changes: dict) -> dict:
    floors: dict[int, dict] = {}
    for lv in fc["metadata"]["levels"]:
        floors[lv["level"]] = {"floor": lv["name"], "base_height": lv["base_height"],
                               "floor_height": lv["height"], "rooms": []}
    for f in fc["features"]:
        p = f["properties"]
        if generate_geojson.is_area_room(p):
            floors[p["level"]]["rooms"].append(
                f"{p['room_id']}: {p['name']} [{p['category']}] {p['ratio'] * 100:.1f}% {p['area_m2']}m²"
                + (" (推定)" if p["confidence"] == "estimated" else ""))
    return {
        "footprint_area_m2": fc["metadata"]["footprint_area_m2"],
        "orientation_deg": fc["metadata"]["orientation_deg"],
        "floors": [floors[k] for k in sorted(floors, reverse=True)],
        "changes": {"added": changes["added"], "removed": changes["removed"],
                    "changed": [f"{c['id']}: {', '.join(c['fields'])}" for c in changes["changed"]]},
    }


def cmd_build(args) -> int:
    path = Path(args.config).resolve()
    try:
        cfg = config.load(path)
    except config.ConfigError as e:
        _print_json({"ok": False, "stage": "validate", "errors": e.errors})
        return 1
    out = _out_dir(cfg.slug)
    out.mkdir(parents=True, exist_ok=True)
    try:
        footprint, info = resolve_footprint(cfg.building["source"], out / "footprint_cache.geojson",
                                            refresh=args.refresh, base_dir=path.parent)
    except (FetchError, OSError) as e:
        _print_json({"ok": False, "stage": "fetch", "error": str(e)})
        return 1

    fc = generate_geojson.generate(cfg, footprint)
    fc["metadata"]["footprint_source"] = generate_geojson.footprint_source(cfg.building["source"], path.parent)
    fc["metadata"]["osm"] = {k: info.get(k) for k in ("osm_type", "osm_id", "name")}
    old = generate_geojson.feature_collection_from_file(out / "indoor.geojson")
    changes = generate_geojson.diff(old, fc)

    token = args.token or os.environ.get("MAPBOX_ACCESS_TOKEN")
    if token and not token.startswith("pk."):
        print("warning: the token is embedded in index.html; use a public pk.* token", file=sys.stderr)

    # changes.json first: the preview server reads it when indoor.geojson changes.
    _write_atomic(out / "changes.json", json.dumps(changes, ensure_ascii=False, indent=2))
    cropped = _crop_basemaps(fc, cfg, footprint, out / "sources" / "basemaps", path.parent)
    images, image_warnings = build_preview.basemap_images(fc, out / "sources" / "basemaps", base_dir=path.parent,
                                                          files=cropped)
    for w in image_warnings:
        print(f"warning: {w}", file=sys.stderr)
    build_preview.write(fc, out / "index.html", token, images)
    # The live page loads images of basemaps added after it was opened from here.
    (out / "images.json").write_text(json.dumps(images), encoding="utf-8")
    (out / "build_info.json").write_text(json.dumps(
        {"config_path": str(path), "slug": cfg.slug, "config_hash": fc["metadata"]["config_hash"],
         "generated_at": fc["metadata"]["generated_at"]}, ensure_ascii=False, indent=2), encoding="utf-8")
    cost = _write_cost(cfg.slug)  # before indoor.geojson, whose change makes the live page re-read it
    quality_report = quality.report(fc)
    quality_before = quality.shares_from_file(out / "quality.json")  # read before it is overwritten
    _write_atomic(out / "quality.json", json.dumps(quality_report, ensure_ascii=False, indent=2))
    _write_atomic(out / "indoor.geojson", json.dumps(fc, ensure_ascii=False))
    snapshot = history.record(path, cfg.slug)

    result = {"ok": True, "slug": cfg.slug,
              "outputs": {"geojson": str(out / "indoor.geojson"), "html": str(out / "index.html")},
              "building": {"name": cfg.building["name"], **{k: info.get(k) for k in ("osm_type", "osm_id", "name", "area_m2")}},
              "history_snapshot": str(snapshot) if snapshot else None,
              "estimated_cost_usd": cost and cost["usd"],
              "quality_area_pct": {g: v["area_pct"] for g, v in quality_report["total"].items()},
              "quality_change": quality.change(quality_before, quality_report),
              **summarise(fc, changes)}
    _print_json(result)
    return 0


def _write_cost(slug: str, report: dict | None = None) -> dict | None:
    """output/<slug>/cost.json from the session logs. A side note: never fails the caller."""
    try:
        report = report or usage_cost.estimate(ROOT, usage_cost.known_slugs(ROOT))
        cost = usage_cost.for_slug(report, slug)
        _write_atomic(_out_dir(slug) / "cost.json", json.dumps(cost, ensure_ascii=False, indent=2))
        return cost
    except Exception as e:
        print(f"warning: cost estimate skipped ({e})", file=sys.stderr)
        return None


def cmd_publish(args) -> int:
    """All built buildings -> one Mapbox tileset. Dry run (writes output/_publish/) unless --upload."""
    from indoor_gen import publish
    cfg = publish.load_settings(ROOT)
    username = args.username or cfg["username"]
    try:
        target, tileset = publish.resolve_target(cfg, args.target, args.tileset)
    except publish.PublishError as e:
        _print_json({"ok": False, "error": str(e)})
        return 1
    only = set(args.only.split(",")) if args.only else None
    buildings, skipped = publish.collect(OUTPUT_ROOT, ROOT, set(args.exclude.split(",")) if args.exclude else set(), only)
    if not buildings:
        _print_json({"ok": False, "error": "no built buildings (run build first)"})
        return 1
    warnings = quality.publish_warnings(buildings, args.warn_weak_pct)
    for w in warnings:
        print(f"warning: {w['message']}", file=sys.stderr)
    ndjson, counts = publish.to_ndjson(buildings)
    dest = OUTPUT_ROOT / "_publish"
    dest.mkdir(parents=True, exist_ok=True)
    _write_atomic(dest / f"{tileset}.ndjson", ndjson)
    result = {"ok": True, "uploaded": False, "target": target, "tileset": f"{username}.{tileset}" if username else tileset,
              "buildings": counts, "skipped_samples": skipped, "bytes": len(ndjson.encode("utf-8")),
              "ndjson": str(dest / f"{tileset}.ndjson"), "quality_warnings": warnings}
    if not args.upload:
        result["next"] = ("dry run; add --upload (needs a publish.json username and a secret "
                          "token with tilesets:write/read/list in $MAPBOX_UPLOAD_TOKEN)")
        _print_json(result)
        return 0
    token = os.environ.get("MAPBOX_UPLOAD_TOKEN")
    if not username or not token:
        _print_json({**result, "ok": False, "error": "need a username (--username or publish.json) "
                                                     "and a secret token in $MAPBOX_UPLOAD_TOKEN"})
        return 1
    if not token.startswith("sk."):
        _print_json({**result, "ok": False, "error": "$MAPBOX_UPLOAD_TOKEN must be a secret sk.* token with tilesets scopes"})
        return 1
    tileset_id = f"{username}.{tileset}"
    mts = publish.Mts(username, token)
    try:
        mts.put_source(tileset, ndjson)
        state = mts.upsert_tileset(tileset_id, publish.recipe(username, tileset, cfg["minzoom"], cfg["maxzoom"]),
                                   "Indoor maps")
        job_id = mts.publish(tileset_id)
        result.update(uploaded=True, tileset_id=tileset_id, tileset=state, job_id=job_id)
        if not args.no_wait:
            job = mts.wait(tileset_id, job_id)
            result["job"] = {"stage": job.get("stage"), "errors": job.get("errors")}
            result["ok"] = job.get("stage") == "success"
    except publish.PublishError as e:
        _print_json({**result, "ok": False, "error": str(e)})
        return 1
    result["style_snippet"] = publish.style_snippet(username, tileset, publish.structure_ids(buildings))
    _print_json(result)
    return 0 if result["ok"] else 1


def cmd_viewer(args) -> int:
    """output/_publish/viewer.html: the published tileset in an official-style indoor map (to check an upload)."""
    from indoor_gen import publish
    cfg = publish.load_settings(ROOT)
    username = args.username or cfg["username"]
    try:
        _, tileset = publish.resolve_target(cfg, args.target, args.tileset)
    except publish.PublishError as e:
        _print_json({"ok": False, "error": str(e)})
        return 1
    buildings, _ = publish.collect(OUTPUT_ROOT, ROOT)
    token = os.environ.get("MAPBOX_ACCESS_TOKEN")
    if token and not token.startswith("pk."):
        print("error: MAPBOX_ACCESS_TOKEN must be a public pk.* token (it is embedded in the page)", file=sys.stderr)
        return 1
    if not buildings or not username:
        _print_json({"ok": False, "error": "need built buildings and a username (publish.json)"})
        return 1
    template = (ROOT / "indoor_gen" / "templates" / "tileset_viewer.html").read_text(encoding="utf-8")
    dest = OUTPUT_ROOT / "_publish" / f"viewer-{tileset}.html"
    dest.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(dest, publish.viewer_html(template, username, tileset, buildings, token))
    url = f"http://127.0.0.1:{args.port}/{dest.name}"
    _print_json({"ok": True, "tileset": f"{username}.{tileset}", "html": str(dest), "url": url})
    if args.serve:  # http rather than file://: a token with URL restrictions, or a browser, may refuse file pages
        from functools import partial
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), partial(SimpleHTTPRequestHandler, directory=str(dest.parent)))
        print(f"Serving {dest.parent} at {url}", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


def cmd_cost(args) -> int:
    report = usage_cost.estimate(ROOT, usage_cost.known_slugs(ROOT))
    built = [p.name for p in OUTPUT_ROOT.iterdir() if (p / "index.html").exists()] if OUTPUT_ROOT.is_dir() else []
    for slug in built:
        _write_cost(slug, report)
    if args.target:
        slug, _ = _slug_from_arg(args.target)
        _print_json(usage_cost.for_slug(report, slug))
        return 0
    _print_json({**{k: v for k, v in report.items() if k != "buckets"},
                 "buckets": {name: {k: b[k] for k in ("usd", "turns", "first", "last")}
                             for name, b in report["buckets"].items()}})
    return 0


def cmd_report(args) -> int:
    d = Path(args.target) if (Path(args.target) / "indoor.geojson").exists() else _out_dir(_slug_from_arg(args.target)[0])
    fc = generate_geojson.feature_collection_from_file(d / "indoor.geojson")
    if fc is None:
        print(f"error: no indoor.geojson for {args.target}; run build first", file=sys.stderr)
        return 1
    rep = quality.report(fc)
    _write_atomic(d / "quality.json", json.dumps(rep, ensure_ascii=False, indent=2))
    if args.json:
        _print_json(rep)
    else:
        print(quality.format_report(rep, args.top))
    return 0


def cmd_compare(args) -> int:
    from indoor_gen.compare import compare

    dirs = []
    for target in (args.a, args.b):
        d = Path(target) if (Path(target) / "indoor.geojson").exists() else _out_dir(_slug_from_arg(target)[0])
        if not (d / "indoor.geojson").exists():
            print(f"error: no indoor.geojson for {target}; give a slug or an output directory", file=sys.stderr)
            return 1
        dirs.append(d)
    _print_json(compare(*dirs))
    return 0


def _crop_basemaps(fc: dict, cfg, footprint, cache_dir: Path, base_dir: Path) -> dict[str, Path]:
    """Show each basemap trimmed to its floor's extent plus a margin (legends and blank margins stay out of the
    map). The full image stays as `source_url` for the alignment pane. Returns {cropped url: file}."""
    from shapely.geometry import shape

    from indoor_gen import crop
    from indoor_gen.geo_utils import LocalFrame

    files = {}
    frame = None
    for bm in fc["metadata"].get("basemaps", []):
        slab = next((f["geometry"] for f in fc["features"]
                     if f["properties"].get("type") == "floor" and f["properties"].get("level") == bm["level"]), None)
        region = shape(slab) if slab else shape(fc["metadata"]["footprint"])
        frame = frame or LocalFrame(footprint, cfg.building.get("crs", "auto"), cfg.building.get("orientation", "auto"))
        try:
            src = build_preview.image_file(bm["url"], cache_dir, base_dir)
            result = crop.cropped_basemap(src, bm["corners"], frame, region, cache_dir)
        except Exception as e:  # the full image is still a fine basemap
            print(f"warning: basemap for level {bm['level']} not trimmed ({e})", file=sys.stderr)
            continue
        if result is None:
            continue
        path, corners, rect = result
        url = f"cropped/{path.name}"
        bm.update(source_url=bm.get("source_url") or bm["url"], url=url, corners=corners, crop_px=list(rect))
        files[url] = path
    return files


def _slug_from_arg(arg: str) -> tuple[str, Path | None]:
    p = Path(arg)
    if p.suffix == ".json" and p.exists():
        return config.slug_for(p, config.load_raw(p)), p
    return arg, None


def cmd_serve(args) -> int:
    from indoor_gen.server import serve
    slug, _ = _slug_from_arg(args.target)
    out = _out_dir(slug)
    if not (out / "index.html").exists():
        print(f"error: {out}/index.html not found; run build first", file=sys.stderr)
        return 1
    serve(out, args.host, args.port)
    return 0


def cmd_undo(args) -> int:
    path = Path(args.config).resolve()
    slug = config.slug_for(path, config.load_raw(path))
    try:
        restored = history.undo(path, slug)
    except RuntimeError as e:
        _print_json({"ok": False, "error": str(e)})
        return 1
    print(f"restored {restored.name}", file=sys.stderr)
    args.refresh = False
    return cmd_build(args)


def cmd_landmarks(args) -> int:
    """Named entrances / ticket gates around the building, for aligning a floor-map image automatically."""
    from indoor_gen.fetch_building import landmarks

    path = Path(args.config).resolve()
    raw = config.load_raw(path)
    slug = config.slug_for(path, raw)
    try:
        footprint, _ = resolve_footprint(raw["building"]["source"], _out_dir(slug) / "footprint_cache.geojson",
                                         base_dir=path.parent)
        found = landmarks(footprint, args.margin)
    except (FetchError, OSError) as e:
        _print_json({"ok": False, "error": str(e)})
        return 1
    _print_json({"ok": True, "landmarks": found,
                 "next": "find the same names on the floor-map image (their gate / door symbol, not the text label), "
                         "write the pairs to output/<slug>/alignment.json and run `align --apply`"})
    return 0


def cmd_parts(args) -> int:
    """List OSM building:part shapes inside the footprint and save them as outline files."""
    from shapely.geometry import mapping

    path = Path(args.config).resolve()
    raw = config.load_raw(path)
    slug = config.slug_for(path, raw)
    try:
        footprint, _ = resolve_footprint(raw["building"]["source"], _out_dir(slug) / "footprint_cache.geojson",
                                         base_dir=path.parent)
        parts = building_parts(footprint)
    except (FetchError, OSError) as e:
        _print_json({"ok": False, "error": str(e)})
        return 1
    out_dir = path.parent / "plans" / slug / "parts"
    listed = []
    for info, geom in parts:
        f = out_dir / f"{info['osm_type']}-{info['osm_id']}.geojson"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"type": "Feature", "properties": info, "geometry": mapping(geom)},
                                ensure_ascii=False), encoding="utf-8")
        listed.append({**info, "outline": str(f.relative_to(path.parent))})
    _print_json({"ok": True, "parts": listed,
                 "hint": "use a part as a floor's \"outline\" for the floors it covers (see height / building:levels)"})
    return 0


def _import_setup(args):
    """Footprint frame and fit target shared by the floor-guide importers; None after printing an error."""
    from indoor_gen.generate_geojson import _polygonal
    from indoor_gen.geo_utils import LocalFrame

    path = Path(args.config).resolve()
    raw = config.load_raw(path)
    slug = config.slug_for(path, raw)
    out = _out_dir(slug)
    try:
        footprint, _ = resolve_footprint(raw["building"]["source"], out / "footprint_cache.geojson",
                                         base_dir=path.parent)
    except (FetchError, OSError) as e:
        _print_json({"ok": False, "stage": "fetch", "error": str(e)})
        return None
    frame = LocalFrame(footprint, raw["building"].get("crs", "auto"), raw["building"].get("orientation", "auto"))
    local = _polygonal(frame.to_local(footprint))
    target = local
    if args.fit_to:
        outline, problem = config.load_outline(path.parent / args.fit_to)
        if problem:
            _print_json({"ok": False, "error": problem})
            return None
        from shapely.geometry import shape as to_shape
        target = _polygonal(local.intersection(frame.to_local(to_shape(outline))))
    excludes: dict[str, set[int]] = {}
    for item in args.exclude or []:
        fname, nums = item.split(":", 1)
        excludes[fname.upper()] = {int(n) for n in nums.split(",") if n}
    return path, slug, out, frame, target, excludes


def _write_plans(analyses, importer, path, slug, out, frame, target, args, extra=None) -> int:
    from shapely.geometry import shape

    from indoor_gen.generate_geojson import _polygonal
    from indoor_gen.importers import common

    plan_dir = path.parent / "plans" / slug
    plan_dir.mkdir(parents=True, exist_ok=True)
    (out / "plan_debug").mkdir(parents=True, exist_ok=True)
    common.share_frames(analyses)
    reports = []
    for a in analyses:
        fl = a["floor"]
        fc, rep = importer.build_plan(a, target, frame.to_wgs84, args.map_up, args.source_id)
        plan_path = plan_dir / f"{fl['name']}.geojson"
        plan_path.write_text(json.dumps(fc, ensure_ascii=False, indent=1), encoding="utf-8")
        drawn = [(str(f["properties"].get("map_no", "")), _polygonal(frame.to_local(shape(f["geometry"]))))
                 for f in fc["features"] if f["geometry"]["type"] != "Point"]  # pictogram points are not drawn
        (out / "plan_debug" / f"{fl['name']}.svg").write_text(common.debug_svg(target, drawn), encoding="utf-8")
        rep["plan"] = str(plan_path.relative_to(path.parent))
        rep["debug_drawing"] = str(out / "plan_debug" / f"{fl['name']}.svg")
        if extra:
            extra(fl, rep)
        reports.append(rep)
    _print_json({"ok": True, "floors": reports,
                 "next": "add \"plan\": \"<plan>\" to each floor in the config, then validate and build"})
    return 0


def cmd_import_imagemap(args) -> int:
    from indoor_gen.importers import imagemap_floorguide as im

    setup = _import_setup(args)
    if setup is None:
        return 1
    path, slug, out, frame, target, excludes = setup
    analyses = []
    for item in args.page:
        name, url = item.split("=", 1)
        name = name.upper()
        try:
            fl = im.fetch_floor(url, name, out / "sources" / "imagemap", refresh=args.refresh)
        except (ValueError, OSError, im.requests.RequestException) as e:
            _print_json({"ok": False, "stage": "fetch", "floor": name, "error": str(e)})
            return 1
        analyses.append(im.analyze_floor(fl, target, args.map_up, excludes.get(name, set())))

    def tenant_list(fl, rep):
        rep["tenant_list"] = [f"{t['no']:02d} {t['name']}" for t in fl["tenants"]]
    return _write_plans(analyses, im, path, slug, out, frame, target, args, tenant_list)


def _image_size(path: Path) -> tuple[float, float]:
    """Pixel size as a browser reports it (naturalWidth/Height); SVGs use their width/height or viewBox."""
    if path.suffix.lower() == ".svg":
        from svgelements import SVG
        svg = SVG.parse(str(path))
        return float(svg.width), float(svg.height)
    from PIL import Image
    with Image.open(path) as im:
        return float(im.width), float(im.height)


def _floor_name(path: Path, level: int) -> str:
    try:
        return next((f.name for f in config.load(path).floors if f.level == level), config.default_floor_name(level))
    except config.ConfigError:
        return config.default_floor_name(level)


def _basemap_snippet(image: str, t: list[float], size, frame, source_id: str | None, alignment: dict | None = None) -> dict:
    from indoor_gen import align
    bm = {"image": image,
          "corners": [[round(c, 7) for c in frame.to_wgs84(p).coords[0]] for p in align.corners(t, *size)]}
    if source_id:
        bm["source"] = source_id
    if alignment:
        bm["alignment"] = alignment
    return bm


def cmd_basemap(args) -> int:
    """Register a floor-map image and print a rough first placement for it.

    A URL stays a URL in the config (the image is cached under output/, never committed);
    a local file is copied next to the config."""
    import shutil

    import requests
    from shapely.geometry import Point

    from indoor_gen import align

    args.exclude = None
    setup = _import_setup(args)
    if setup is None:
        return 1
    path, slug, out, frame, target, _ = setup
    src = args.image
    suffix = Path(src.split("?")[0]).suffix.lower()
    if suffix not in build_preview.MIME:
        _print_json({"ok": False, "error": f"unsupported image type '{suffix}' (use {', '.join(build_preview.MIME)}; "
                                           "convert a PDF page first, e.g. `sips -s format png in.pdf --out out.png`)"})
        return 1
    name = _floor_name(path, args.level)
    try:
        if src.startswith(("http://", "https://")):
            image, local = src, build_preview.image_file(src, out / "sources" / "basemaps")
        else:
            local = path.parent / "plans" / slug / "maps" / f"{name}{suffix}"
            local.parent.mkdir(parents=True, exist_ok=True)
            if Path(src).expanduser().resolve() != local.resolve():
                shutil.copyfile(Path(src).expanduser(), local)
            image = str(local.relative_to(path.parent))
        size = _image_size(local)
    except (requests.RequestException, OSError, ValueError) as e:
        _print_json({"ok": False, "error": str(e)})
        return 1
    # Scale, turn and move the drawing (never bend it) so its outline covers the footprint best.
    like_angle = None
    if args.like_level is not None:
        ref = next((f for f in config.load_raw(path)["floors"]
                    if f.get("level") == args.like_level or args.like_level in f.get("levels", [])), None)
        corners = (ref or {}).get("basemap", {}).get("corners")
        if not corners:
            _print_json({"ok": False, "error": f"level {args.like_level} has no placed floor map to take the orientation from"})
            return 1
        (x0, y0), (x1, y1) = (frame.to_local(Point(c)).coords[0] for c in corners[:2])
        like_angle = math.degrees(math.atan2(y1 - y0, x1 - x0)) % 360
    warnings = []
    try:
        outline = None
        if args.outline_trace:
            from shapely.geometry import Polygon as _Poly
            from shapely.ops import unary_union
            zones = json.loads(Path(args.outline_trace).read_text())
            outline = unary_union([_Poly(z["px"]).buffer(0) for z in zones if z.get("px")]).convex_hull
        t, score, notes = align.auto_fit(local, target, args.map_up, like_angle, args.tile_map, args.north_deg, frame.angle,
                                         outline)
        fit = {"method": "outline_fit", "iou": round(score, 3), **notes}
        if notes.get("ambiguous"):
            warnings.append("the overlap cannot tell which way is up (two placements, "
                            f"{notes['candidates'][0]['angle']}° and {notes['candidates'][1]['angle']}°, overlap almost the same): "
                            "pass --map-up, or --like-level <a floor already placed>, and check the result")
        if notes.get("off_north_deg", 0) > 20:
            warnings.append(f"the placement that overlaps best is {notes['off_north_deg']:.0f}° away from the north "
                            f"arrow's direction ({args.north_deg:g}°): check the angle you read, or the footprint")
        if score < 0.4:
            warnings.append(f"the drawing's outline overlaps the footprint by only {score:.0%}: check the placement "
                            "(map-up, the footprint, or a legend / site plan around the building)")
    except (ValueError, OSError) as e:
        t = align.initial_transform(target, *size, map_up=args.map_up or "north")
        fit = {"method": "bbox_fit", "reason": str(e)}
        warnings.append(f"the drawing's outline could not be read ({e}): this is only a rough bounding-box placement")
    _print_json({"ok": True, "level": args.level, "floor": name, "image_size": size, "fit": fit,
                 "basemap": _basemap_snippet(image, t, size, frame, args.source_id),
                 "warnings": warnings,
                 "next": "set this as the floor's \"basemap\" in the config and build; if the placement is off, "
                         "pass --map-up / --fit-to, or fix it with two point pairs (similarity) in the preview"})
    return 0


def cmd_align(args) -> int:
    """Turn the point pairs saved by the preview (alignment.json) into basemap corners."""
    if args.reset is not None:
        return _reset_basemap(Path(args.config).resolve(), args.reset)
    from shapely.geometry import Point

    from indoor_gen import align

    args.fit_to = None
    args.exclude = None
    setup = _import_setup(args)
    if setup is None:
        return 1
    path, slug, out, frame, _, _ = setup
    apath = out / "alignment.json"
    if not apath.exists():
        _print_json({"ok": False, "error": f"{apath} not found; pick point pairs in the preview and press 保存 first"})
        return 1
    a = json.loads(apath.read_text(encoding="utf-8"))
    px = [tuple(p["px"]) for p in a["pairs"]]
    xy = [frame.to_local(Point(p["lnglat"])).coords[0] for p in a["pairs"]]
    mode = a.get("mode", "similarity")
    if mode != "similarity":
        _print_json({"ok": False, "error": f"alignment mode '{mode}' is no longer supported: the floor map is only "
                                           "scaled, turned and moved (use 'similarity')"})
        return 1
    try:
        t = align.fit_pairs(px, xy, mode)
    except ValueError as e:
        _print_json({"ok": False, "error": str(e)})
        return 1
    res = align.residuals(t, px, xy)
    rms = (sum(r * r for r in res) / len(res)) ** 0.5
    summary = {"mode": mode, "rms_m": round(rms, 2), "saved_at": a.get("saved_at"),
               "pairs": [{"px": [round(v, 1) for v in p["px"]], "lnglat": p["lnglat"],
                          **({"label": p["label"]} if p.get("label") else {})} for p in a["pairs"]]}
    floor = next((f for f in config.load(path).floors if f.level == a["level"]), None)
    warnings = []
    if len(res) == align.MIN_PAIRS[mode]:
        warnings.append("only the minimum number of pairs: the residuals cannot show a misplaced point; add one more")
    if max(res) > 2.0:
        warnings.append(f"pair {res.index(max(res)) + 1} is {max(res):.1f} m off the fit; check it")
    if floor is not None and floor.basemap and floor.basemap["url"] == a["image"]:
        # Map points that sit where the image is already drawn were clicked on the overlay itself.
        now = [frame.to_local(Point(c)).coords[0] for c in floor.basemap["corners"]]
        w, h = a["image_size"]
        t_now = align.fit_pairs([(0, 0), (w, 0), (w, h), (0, h)], now, "affine")
        if max(align.residuals(t_now, px, xy)) < 1.5:
            warnings.append("every map point lies where the image is already drawn (within 1.5 m): the points were "
                            "probably clicked on the overlaid image, so this keeps the current placement. "
                            "Pick the map points on the base map / OSM outline instead")
    if floor is not None and floor.has_plan:
        warnings.append("this floor's rooms come from a plan; moving the image does not move them")
    result = {"ok": True, "level": a["level"], "floor": floor.name if floor else None,
              "residuals_m": [round(r, 2) for r in res], "rms_m": summary["rms_m"], "warnings": warnings,
              "basemap": _basemap_snippet(a["image"], t, a["image_size"], frame, a.get("source"), summary)}
    if not args.apply:
        _print_json({**result, "next": "replace the floor's \"basemap\" in the config with this, then build"})
        return 0
    return _apply_basemap(path, a["level"], result)


def cmd_edit_rooms(args) -> int:
    """Apply the room-shape edits saved by the preview (room_edits.json) to the config / plan and build."""
    from indoor_gen import room_edit

    path = Path(args.config).resolve()
    try:
        slug = config.load(path).slug
    except config.ConfigError as e:
        _print_json({"ok": False, "stage": "validate", "errors": e.errors})
        return 1
    epath = _out_dir(slug) / "room_edits.json"
    if not epath.exists():
        _print_json({"ok": False, "error": f"{epath} not found; edit rooms in the preview and press 保存 first"})
        return 1
    try:
        body = json.loads(epath.read_text(encoding="utf-8"))
        raw = config.load_raw(path)
        res = room_edit.apply_edits(path, raw, int(body["level"]), list(body["ops"]))
    except room_edit.EditError as e:
        _print_json({"ok": False, "error": str(e)})
        return 1
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
        _print_json({"ok": False, "error": f"room_edits.json must be {{\"level\": int, \"ops\": [...]}} ({e})"})
        return 1
    epath.unlink(missing_ok=True)
    return _write_and_build(path, {"ok": True, "level": body["level"], **res}, raw)


def cmd_export_floor(args) -> int:
    """Write one floor's rooms as GeoJSON for an external editor (geojson.io); `import-floor` reads it back."""
    from indoor_gen import geojsonio, room_edit

    path = Path(args.config).resolve()
    try:
        slug = config.load(path).slug
        fc = geojsonio.export_floor(_out_dir(slug) / "indoor.geojson", slug, args.level)
    except (config.ConfigError, room_edit.EditError, OSError) as e:
        _print_json({"ok": False, "error": str(e)})
        return 1
    out = Path(args.output) if args.output else _out_dir(slug) / f"floor-{args.level}.geojson"
    out.write_text(json.dumps(fc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _print_json({"ok": True, "file": str(out), "rooms": len(fc["features"]),
                 "next": "edit it (geojson.io: drag the file in), save, then `main.py import-floor <config> --file <edited>`"})
    return 0


def cmd_import_floor(args) -> int:
    """Apply a floor GeoJSON edited elsewhere: moved / renamed rooms, new rooms (no `id`), removed rooms; then build."""
    from indoor_gen import geojsonio, room_edit

    path = Path(args.config).resolve()
    try:
        slug = config.load(path).slug
        edited = json.loads(Path(args.file).read_text(encoding="utf-8"))
        meta = edited.get("metadata") or {} if isinstance(edited, dict) else {}
        level = args.level if args.level is not None else meta.get("level")
        if not isinstance(level, int):
            raise room_edit.EditError("the floor level is unknown: pass --level, or use a file written by export-floor")
        if meta.get("slug") not in (None, slug):
            raise room_edit.EditError(f"the file is from {meta.get('slug')!r}, not {slug!r}")
        ops = geojsonio.diff_to_ops(_out_dir(slug) / "indoor.geojson", level, edited)
        count = lambda k: sum(o["op"] == k for o in ops)
        summary = {"update": count("update"), "add": count("add"), "delete": count("delete")}
        if args.dry_run or not ops:
            _print_json({"ok": True, "dry_run": True, "level": level, "summary": summary,
                         "ops": [{k: v for k, v in o.items() if k != "geometry"} for o in ops]})
            return 0
        raw = config.load_raw(path)
        res = room_edit.apply_edits(path, raw, level, ops)
    except (config.ConfigError, room_edit.EditError, OSError, json.JSONDecodeError) as e:
        _print_json({"ok": False, "error": str(e)})
        return 1
    return _write_and_build(path, {"ok": True, "level": level, "summary": summary, **res}, raw)


def cmd_mapbox_footprint(args) -> int:
    """Replace the OSM footprint by the Mapbox Building's shape and move the plans / floor maps to match."""
    from indoor_gen import history, mapbox_building, realign

    path = Path(args.config).resolve()
    try:
        cfg = config.load(path)
        raw = config.load_raw(path)
        out = _out_dir(cfg.slug)
        if raw["building"]["source"].get("type") == "geojson" and not (args.force or args.dry_run):
            _print_json({"ok": False, "error": "the footprint is already a GeoJSON file (drawn or measured by hand?); "
                                               "--force replaces it by the Mapbox Building's shape"})
            return 1
        old_fp, _ = resolve_footprint(raw["building"]["source"], out / "footprint_cache.geojson", base_dir=path.parent)
        new_fp, info = mapbox_building.mapbox_footprint(old_fp, args.token)
        res = realign.fit(raw, path.parent, new_fp) if (args.realign or args.dry_run) else {"moved": False, "t": None}
    except (config.ConfigError, FetchError) as e:
        _print_json({"ok": False, "error": str(e) if isinstance(e, FetchError) else e.errors})
        return 1
    report = {"ok": True, "footprint": info,
              "realign": {k: v for k, v in res.items() if k not in ("t", "frame")}}
    if args.dry_run:
        _print_json({**report, "dry_run": True})
        return 0
    history.record(path, cfg.slug)
    fp_rel = f"plans/{cfg.slug}/footprint.geojson"
    fp_path = path.parent / fp_rel
    fp_path.parent.mkdir(parents=True, exist_ok=True)
    fp_path.write_text(json.dumps(mapbox_building.footprint_feature(new_fp, info), ensure_ascii=False) + "\n", encoding="utf-8")
    report["realign"]["changed"] = realign.apply(raw, path.parent, res, out)
    raw["building"]["source"] = {"type": "geojson", "path": fp_rel}
    return _write_and_build(path, report, raw)


def cmd_nudge_map(args) -> int:
    """Move every plan and floor map of a building by a small hand-set amount (rotation, scale, shift)."""
    from indoor_gen import history, realign

    path = Path(args.config).resolve()
    try:
        cfg = config.load(path)
        raw = config.load_raw(path)
        fp, _ = resolve_footprint(raw["building"]["source"], _out_dir(cfg.slug) / "footprint_cache.geojson", base_dir=path.parent)
    except (config.ConfigError, FetchError) as e:
        _print_json({"ok": False, "error": str(e) if isinstance(e, FetchError) else e.errors})
        return 1
    res = realign.manual(raw, path.parent, fp, args.rotate_deg, args.scale, args.east_m, args.north_m)
    history.record(path, cfg.slug)
    changed = realign.apply(raw, path.parent, res, _out_dir(cfg.slug), level=args.level)
    return _write_and_build(path, {"ok": True, "changed": changed}, raw)


def _apply_basemap(path: Path, level: int, result: dict) -> int:
    """Write the fitted basemap onto the floor's entry in the config and build (as one JSON report)."""
    raw = config.load_raw(path)
    entry = next((f for f in raw["floors"] if f.get("level") == level or level in f.get("levels", [])), None)
    if entry is None:
        _print_json({**result, "ok": False, "error": f"no floor entry for level {level} in the config"})
        return 1
    if "levels" in entry:
        result["warnings"].append(f"level {level} is defined together with levels {entry['levels']}; they share this basemap")
    bm = result["basemap"]
    old = entry.get("basemap") or {}
    if old.get("source") and "source" not in bm:
        bm = {**bm, "source": old["source"]}
    # Keep the automatic placement from before the first alignment, for 「自動配置に戻す」.
    if old.get("auto_corners"):
        bm = {**bm, "auto_corners": old["auto_corners"]}
    elif old.get("corners") and "alignment" not in old and old.get("image") == bm["image"]:
        bm = {**bm, "auto_corners": old["corners"]}
    entry["basemap"] = {k: bm[k] for k in ("image", "corners", "source", "alignment", "auto_corners") if k in bm}
    return _write_and_build(path, {**result, "applied": True}, raw)


def _brief_changes(changes, keep: int = 10):
    """The build's diff for the JSON report: counts, and the first `keep` ids of each list (a realign changes thousands)."""
    if not isinstance(changes, dict):
        return changes
    out = {}
    for k, v in changes.items():
        if isinstance(v, list) and len(v) > keep:
            out[k] = v[:keep] + [f"... {len(v) - keep} more"]
        else:
            out[k] = v
    return out


def _write_and_build(path: Path, report: dict, raw: dict | None = None) -> int:
    import contextlib
    import io

    if raw is not None:
        _write_atomic(path, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cmd_build(argparse.Namespace(config=str(path), refresh=False, token=None))
    build = json.loads(out.getvalue())
    _print_json({**report, "ok": code == 0,
                 "build": {k: (_brief_changes(build[k]) if k == "changes" else build[k])
                           for k in ("ok", "stage", "errors", "error", "changes", "history_snapshot") if k in build}})
    return code


def _reset_basemap(path: Path, level: int) -> int:
    """Undo point-pair alignment: back to the recorded automatic placement, or the plan's own basemap."""
    raw = config.load_raw(path)
    entry = next((f for f in raw["floors"] if f.get("level") == level or level in f.get("levels", [])), None)
    bm = (entry or {}).get("basemap")
    if not bm or "alignment" not in bm:
        _print_json({"ok": False, "level": level, "error": "this floor's map has not been aligned by point pairs"})
        return 1
    if bm.get("auto_corners"):
        entry["basemap"] = {k: v for k, v in bm.items() if k not in ("alignment", "auto_corners")}
        entry["basemap"]["corners"] = bm["auto_corners"]
        how = "auto_corners"
    elif entry.get("plan") or entry.get("copy_from") is not None:
        del entry["basemap"]   # the plan's own basemap (placed by its importer) shows again
        how = "plan"
    else:
        _print_json({"ok": False, "level": level, "error": "no automatic placement is recorded for this floor"})
        return 1
    return _write_and_build(path, {"level": level, "reset": how}, raw)


def cmd_trace(args) -> int:
    """Turn room outlines drawn in a floor-map image's pixels into plan GeoJSON, through that
    floor's alignment (the image corners)."""
    from shapely.geometry import Point, Polygon, mapping

    from indoor_gen import align
    from indoor_gen.geo_utils import LocalFrame

    path = Path(args.config).resolve()
    raw = config.load_raw(path)
    slug = config.slug_for(path, raw)
    entry = next((f for f in raw["floors"] if f.get("level") == args.level or args.level in f.get("levels", [])), None)
    bm = (entry or {}).get("basemap")
    if not bm:
        _print_json({"ok": False, "error": f"level {args.level} has no basemap in the config"})
        return 1
    out = _out_dir(slug)
    try:
        footprint, _ = resolve_footprint(raw["building"]["source"], out / "footprint_cache.geojson", base_dir=path.parent)
        size = _image_size(build_preview.image_file(bm["image"], out / "sources" / "basemaps", path.parent))
    except (FetchError, OSError) as e:
        _print_json({"ok": False, "error": str(e)})
        return 1
    frame = LocalFrame(footprint, raw["building"].get("crs", "auto"), raw["building"].get("orientation", "auto"))
    al = bm.get("alignment") or {}
    w, h = size
    t = align.fit_pairs([(0, 0), (w, 0), (w, h), (0, h)],
                        [frame.to_local(Point(c)).coords[0] for c in bm["corners"]], "affine")
    to_local = lambda p: align.georef.apply(Point(p), t).coords[0]  # noqa: E731
    # Without point pairs the image sits where it was placed automatically (fitted to the footprint).
    georef = ({"method": "point_pairs", "mode": al["mode"], "rms_m": al.get("rms_m"), "pairs": len(al.get("pairs", []))}
              if al.get("mode") else {"method": "outline_fit"})
    rooms = json.loads(Path(args.rooms).read_text(encoding="utf-8"))
    local_fp = frame.to_local(footprint)
    features, report = [], []
    for r in rooms:
        if "point" in r:  # a pictogram's position (icon required) becomes a point POI
            pt = Point(to_local(r["point"]))
            props = {k: v for k, v in r.items() if k != "point"}
            props.setdefault("confidence", "sourced")
            features.append({"type": "Feature", "properties": props, "geometry": mapping(frame.to_wgs84(pt))})
            report.append(f"{r['id']}: {r['name']} ({r['icon']}) {'inside' if local_fp.contains(pt) else 'OUTSIDE'} the footprint")
            continue
        poly = Polygon([to_local(p) for p in r["px"]])
        if not poly.is_valid:
            poly = poly.buffer(0)
        props = {k: v for k, v in r.items() if k != "px"}
        props.setdefault("confidence", "sourced")
        features.append({"type": "Feature", "properties": props, "geometry": mapping(frame.to_wgs84(poly))})
        inside = poly.intersection(local_fp).area
        report.append(f"{r['id']}: {r['name']} {poly.area:.0f} m² ({inside / poly.area * 100:.0f}% inside the footprint)")
    fc = {"type": "FeatureCollection",
          "metadata": {"floor": entry.get("name"), "traced_from": bm["image"], "source": bm.get("source"),
                       "alignment": al.get("mode", "corners"), "geometry_source": "traced", "georef": georef},
          "features": features}
    dest = path.parent / "plans" / slug / f"{entry.get('name') or config.default_floor_name(args.level)}.geojson"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(fc, ensure_ascii=False, indent=1), encoding="utf-8")
    _print_json({"ok": True, "plan": str(dest.relative_to(path.parent)), "rooms": report,
                 "next": "set the floor's \"plan\" to this file (drop ratio rooms it replaces), validate and build"})
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("locate", help="list OSM building candidates for a name/address or coordinate")
    p.add_argument("query", nargs="?")
    p.add_argument("--lat", type=float)
    p.add_argument("--lon", type=float)
    p.add_argument("--radius", type=float, default=60)
    p.add_argument("--limit", type=int, default=8)
    p.set_defaults(func=cmd_locate)

    p = sub.add_parser("validate", help="check a config against the schema and ratio rules")
    p.add_argument("config")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("build", help="generate indoor.geojson and index.html")
    p.add_argument("config")
    p.add_argument("--refresh", action="store_true", help="re-fetch the footprint from OSM")
    p.add_argument("--token", help="Mapbox public token (default: $MAPBOX_ACCESS_TOKEN)")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("serve", help="live preview server for a built slug")
    p.add_argument("target", help="slug or config path")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("undo", help="restore the previous built config and rebuild")
    p.add_argument("config")
    p.add_argument("--token")
    p.set_defaults(func=cmd_undo)

    p = sub.add_parser("publish", help="all built buildings -> one Mapbox tileset (dry run unless --upload)")
    p.add_argument("--upload", action="store_true", help="really upload and publish (secret token in $MAPBOX_UPLOAD_TOKEN)")
    p.add_argument("--username", help="Mapbox account (default: publish.json)")
    p.add_argument("--target", help="dev or prod, as named in publish.json (default: its default_target, dev)")
    p.add_argument("--tileset", help="a tileset name instead of a target, e.g. indoor-test")
    p.add_argument("--only", help="comma-separated slugs to include (default: all built, samples skipped)")
    p.add_argument("--exclude", help="comma-separated slugs to leave out")
    p.add_argument("--no-wait", action="store_true", help="do not wait for the publish job to finish")
    p.add_argument("--warn-weak-pct", type=float, default=quality.WEAK_WARN_PCT,
                   help="warn (never block) about buildings whose estimated + guessed-shape room area is above this %% "
                        "(default %(default)g)")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("viewer", help="page that shows the published tileset with IndoorControl (checks an upload)")
    p.add_argument("--username")
    p.add_argument("--target", help="dev or prod, as named in publish.json (default: its default_target, dev)")
    p.add_argument("--tileset", help="a tileset name instead of a target")
    p.add_argument("--serve", action="store_true", help="also serve the page over http://127.0.0.1:<port>/ (until Ctrl-C)")
    p.add_argument("--port", type=int, default=8780)
    p.set_defaults(func=cmd_viewer)

    p = sub.add_parser("cost", help="estimate the Claude usage cost of each indoor map from Claude Code's session logs")
    p.add_argument("target", nargs="?", help="slug or config path (default: every building, development and other)")
    p.set_defaults(func=cmd_cost)

    p = sub.add_parser("report", help="how well each room is sourced, per floor, and the weakest rooms to research next")
    p.add_argument("target", help="slug, config path or output directory")
    p.add_argument("--top", type=int, default=10, help="how many weak rooms to list (default 10)")
    p.add_argument("--json", action="store_true", help="print the full report as JSON (always written to quality.json)")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("compare", help="compare two builds of one building (e.g. made with different models)")
    p.add_argument("a", help="reference: slug, config path or output directory")
    p.add_argument("b", help="candidate: slug, config path or output directory (e.g. in another worktree)")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("parts", help="list OSM building:part shapes (towers, podiums) inside the footprint")
    p.add_argument("config")
    p.set_defaults(func=cmd_parts)

    # site-specific commands: any indoor_gen/importers/*_cli.py with a register() (not every checkout has them)
    for mod in sorted((ROOT / "indoor_gen" / "importers").glob("*_cli.py")):
        importlib.import_module(f"indoor_gen.importers.{mod.stem}").register(
            sub, import_setup=_import_setup, write_plans=_write_plans, print_json=_print_json)

    p = sub.add_parser("import-imagemap",
                       help="import tenant shapes from floor-guide pages drawn as an HTML image map (<img usemap>)")
    p.add_argument("config")
    p.add_argument("--page", action="append", required=True, metavar="FLOOR=URL",
                   help="floor name and its floor-guide page, e.g. 1F=https://example.com/floors/1f.html (repeatable)")
    p.add_argument("--map-up", required=True, choices=["north", "east", "south", "west"],
                   help="compass direction at the top of the floor maps (read the map's edge labels)")
    p.add_argument("--exclude", action="append", metavar="FLOOR:N,N",
                   help="hotspot numbers (map_no) to skip on a floor, e.g. 3F:4")
    p.add_argument("--source-id", help="provenance source id to tag imported rooms with")
    p.add_argument("--fit-to", help="outline GeoJSON (relative to the config) to fit the maps to instead of the "
                                    "whole footprint")
    p.add_argument("--refresh", action="store_true", help="re-download the pages and images")
    p.set_defaults(func=cmd_import_imagemap)

    p = sub.add_parser("trace", help="room outlines in floor-map image pixels -> plan GeoJSON via the floor's alignment")
    p.add_argument("config")
    p.add_argument("--level", type=int, required=True)
    p.add_argument("--rooms", required=True,
                   help="JSON list of {id, name, category, px: [[x, y], ...], ...}; other keys become properties")
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("landmarks", help="named entrances / ticket gates around the building (for auto alignment)")
    p.add_argument("config")
    p.add_argument("--margin", type=float, default=150, help="metres around the footprint to search (default 150)")
    p.set_defaults(func=cmd_landmarks)

    p = sub.add_parser("basemap", help="copy a floor-map image next to the config and print a first placement")
    p.add_argument("config")
    p.add_argument("--level", type=int, required=True)
    p.add_argument("--image", required=True,
                   help="http(s) URL (kept as a URL, cached under output/) or a local image file (copied next to "
                        "the config); png, jpg, webp, svg")
    p.add_argument("--map-up", default=None, choices=["north", "east", "south", "west"],
                   help="compass direction at the top of the image (read it from a compass / street names); "
                        "without it every direction is tried, and a near-symmetric building may be ambiguous")
    p.add_argument("--north-deg", type=float, metavar="DEG",
                   help="where the drawing's north arrow points, in degrees clockwise from the top of the image "
                        "(0 = north is up, 90 = north points right, 249 = lower left); finer than --map-up, and the "
                        "overlap fit starts there and only keeps placements near it")
    p.add_argument("--like-level", type=int, metavar="LEVEL",
                   help="take the orientation from the floor map already placed on this level (same building, "
                        "same drawing style); settles which way is up when the outline overlap cannot")
    p.add_argument("--tile-map", action="store_true",
                   help="the image is a map-app screenshot with terrain / runways around the building (no blank page): "
                        "read the building from its floor colours")
    p.add_argument("--outline-trace", metavar="TRACE.json",
                   help="fit the convex hull of this trace file's `px` outlines to the footprint instead of the image's own "
                        "opaque area (a seating chart on a white page: `tools/svg_zones.py` output)")
    p.add_argument("--fit-to", help="outline GeoJSON (relative to the config) to place the image on, e.g. a tower part")
    p.add_argument("--source-id", help="provenance source id of the image")
    p.set_defaults(func=cmd_basemap)

    p = sub.add_parser("align", help="fit a floor-map image to the point pairs picked in the preview (alignment.json)")
    p.add_argument("config")
    p.add_argument("--apply", action="store_true",
                   help="also write the result into the floor's basemap in the config and build (what the preview's 保存 does)")
    p.add_argument("--reset", type=int, metavar="LEVEL",
                   help="instead, put LEVEL's map back to its automatic placement and build (the preview's 自動配置に戻す)")
    p.set_defaults(func=cmd_align)

    p = sub.add_parser("edit-rooms", help="apply the room-shape edits saved by the preview (room_edits.json) and build")
    p.add_argument("config")
    p.set_defaults(func=cmd_edit_rooms)

    p = sub.add_parser("export-floor", help="write one floor's rooms as GeoJSON to edit in geojson.io (see import-floor)")
    p.add_argument("config")
    p.add_argument("--level", type=int, required=True)
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_export_floor)

    p = sub.add_parser("import-floor", help="apply a floor GeoJSON edited elsewhere (moved / new / removed rooms) and build")
    p.add_argument("config")
    p.add_argument("--file", required=True)
    p.add_argument("--level", type=int)
    p.add_argument("--dry-run", action="store_true", help="only list what would change")
    p.set_defaults(func=cmd_import_floor)

    p = sub.add_parser("mapbox-footprint", help="use the Mapbox Building's shape as the footprint (instead of the OSM outline) "
                                                "and move the plans and floor maps to match")
    p.add_argument("config")
    p.add_argument("--token", help="public Mapbox token (default $MAPBOX_ACCESS_TOKEN)")
    p.add_argument("--realign", action="store_true", help="also move the plans and floor maps (one similarity transform, best overlap with the new footprint); "
                                                         "by default they stay where they are")
    p.add_argument("--force", action="store_true", help="replace a footprint that is already a GeoJSON file (e.g. measured by hand)")
    p.add_argument("--dry-run", action="store_true", help="show the new footprint and how far the plans would move, write nothing")
    p.set_defaults(func=cmd_mapbox_footprint)

    p = sub.add_parser("nudge-map", help="move every plan and floor map of a building (or one floor with --level) by a small hand-set amount "
                                         "(rotation counter-clockwise about the footprint's centre, scale, shift east / north)")
    p.add_argument("config")
    p.add_argument("--rotate-deg", type=float, default=0.0, help="counter-clockwise on the map (negative: clockwise)")
    p.add_argument("--scale", type=float, default=1.0)
    p.add_argument("--east-m", type=float, default=0.0)
    p.add_argument("--north-m", type=float, default=0.0)
    p.add_argument("--level", type=int, default=None,
                   help="move only this floor's plan, outline and map (default: every floor)")
    p.set_defaults(func=cmd_nudge_map)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
