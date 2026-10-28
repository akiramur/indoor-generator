"""`main.py orient <config> --level N`: check which way a placed floor map is turned, by the names printed on it (see orient.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import build_preview, config, orient
from .fetch_building import FetchError, named_places, resolve_footprint


def register(sub, *, root: Path, out_dir, slug_from_arg, write_atomic, print_json) -> None:
    def cmd_orient(args) -> int:
        path = Path(args.config).resolve()
        raw = config.load_raw(path)
        slug = config.slug_for(path, raw)
        floor = next((f for f in raw["floors"] if f.get("level") == args.level or args.level in f.get("levels", [])), None)
        bm = (floor or {}).get("basemap") or {}
        if not bm.get("corners"):
            print(f"error: level {args.level} has no placed floor map (run basemap first)", file=sys.stderr)
            return 1
        name = floor.get("name") or config.default_floor_name(args.level)
        trace_path = Path(args.trace) if args.trace else path.parent / "plans" / slug / f"{name}-trace.json"
        try:
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
            image = bm["image"]
            local = (build_preview.image_file(image, out_dir(slug) / "sources" / "basemaps")
                     if image.startswith(("http://", "https://")) else path.parent / image)
            from PIL import Image
            with Image.open(local) as im:
                size = (im.width, im.height)
            cache = out_dir(slug) / "osm_names.json"
            if cache.exists() and not args.refresh:
                places = json.loads(cache.read_text(encoding="utf-8"))
            else:
                footprint, _ = resolve_footprint(raw["building"]["source"], out_dir(slug) / "footprint_cache.geojson",
                                                 base_dir=path.parent)
                places = named_places(footprint, args.margin)
                cache.parent.mkdir(parents=True, exist_ok=True)
                write_atomic(cache, json.dumps(places, ensure_ascii=False, indent=1))
        except (OSError, json.JSONDecodeError, FetchError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        named = orient.zone_centres(trace)
        rows = orient.score_turns(bm["corners"], size, named, places)
        print_json({"slug": slug, "level": args.level, "named_zones": len(named), "osm_names": len(places),
                    "turns": rows, "verdict": orient.verdict(rows),
                    "how_to_read": "turn_deg is how far to turn the map clockwise from where it is now; 0 = keep it"})
        return 0

    p = sub.add_parser("orient", help="check which way a placed floor map is turned: names on the map vs OSM features")
    p.add_argument("config")
    p.add_argument("--level", type=int, required=True)
    p.add_argument("--trace", help="the floor's *-trace.json (default: configs/plans/<slug>/<floor>-trace.json)")
    p.add_argument("--margin", type=float, default=100, help="metres around the footprint to read OSM names from")
    p.add_argument("--refresh", action="store_true", help="read OSM again instead of output/<slug>/osm_names.json")
    p.set_defaults(func=cmd_orient)
