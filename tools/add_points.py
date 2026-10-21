"""Add facility pictograms (and stairs) read from a floor-map image to a floor, from a list of pixel positions.

The positions are the ones you read off the image (`tools/crop_grid.py`) or accepted from `tools/detect_pictograms.py`.
Two targets:

  trace   append them to `configs/plans/<slug>/<floor>-trace.json` (the pixel outlines `main.py trace` turns into the
          plan through the floor's alignment). Then re-run `main.py trace ... --level N --rooms <that file>`.
  plan    write them straight into `configs/plans/<slug>/<floor>.geojson`, for plans that do not come from `trace`
          (importers): pixels -> lng/lat through the plan's `metadata.basemap.corners` (the image's four corners:
          (0,0) (W,0) (W,H) (0,H)), so give the image / SVG canvas size with --size. Existing features are untouched.

Entries (JSON list) are either a point  {"icon": "elevator", "x": 524, "y": 355, "name": "エレベーター"}  (name defaults
from the icon) or an outline  {"name": "階段", "px": [[x, y], ...]}  (a room, category connector unless given).
Ids are `L<level>-pict-NN` / `L<level>-stairs-NN`; --replace first drops the existing entries with those prefixes, so
the command can be re-run with a corrected list.

    .venv/bin/python tools/add_points.py trace configs/plans/<slug>/1F-trace.json points.json --source s1 --replace
    .venv/bin/python tools/add_points.py plan  configs/plans/<slug>/1F.geojson points.json --size 600x410 --replace
    # points from a detect_pictograms.py run: hit index (the contact sheet's number) : icon
    .venv/bin/python tools/add_points.py trace <trace.json> --from-detect found.json --image maps/1F.png --accept 0:elevator,3:toilet,5:atm
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_NAMES = {"elevator": "エレベーター", "escalator": "エスカレーター", "stairs": "階段", "atm": "ATM", "toilet": "トイレ",
                 "toilet-male": "男性トイレ", "toilet-female": "女性トイレ"}


def level_of(path: Path) -> int:
    """1F -> 1, B1F -> -1 (from a file name like 1F-trace.json / B1F.geojson)."""
    m = re.match(r"^(B?)(\d+)F", path.name, flags=re.IGNORECASE)
    if not m:
        sys.exit(f"cannot read the floor from the file name {path.name}; use --level")
    return -int(m.group(2)) if m.group(1) else int(m.group(2))


def entries_from_detect(detect: Path, image: str | None, accept: str) -> list[dict]:
    data = json.loads(detect.read_text(encoding="utf-8"))
    key = next((k for k in data if image and Path(k).resolve() == Path(image).resolve()), None) if image else None
    if key is None:
        if len(data) != 1:
            sys.exit(f"{detect} holds {len(data)} images; name one with --image")
        key = next(iter(data))
    hits = data[key]
    out = []
    for item in accept.split(","):
        idx, icon = item.split(":")
        h = hits[int(idx)]
        out.append({"icon": icon, "x": h["x"], "y": h["y"]})
    return out


def normalise(entries: list[dict], level: int, source: str | None) -> list[dict]:
    """Trace-style entries ({id, name, [icon], source, point | px}) with ids, in the order given."""
    counts = {"pict": 0, "stairs": 0}
    out = []
    for e in entries:
        if "px" in e:
            if e.get("name") != "階段" and "id" not in e:
                sys.exit(f"an outline entry needs name 階段 or an explicit id: {e}")
            kind, rec = "stairs", {"category": "connector", **e}
            rec["px"] = [[float(a), float(b)] for a, b in e["px"]]
        else:
            if "icon" not in e or "x" not in e or "y" not in e:
                sys.exit(f"an entry needs icon, x and y (or name and px): {e}")
            kind, rec = "pict", {"name": DEFAULT_NAMES.get(e["icon"], e["icon"]), "icon": e["icon"], **e,
                                 "point": [float(e["x"]), float(e["y"])]}
            rec.pop("x"), rec.pop("y")
        counts[kind] += 1
        rec.setdefault("id", f"L{level}-{kind}-{counts[kind]:02d}")
        if source:
            rec.setdefault("source", source)
        out.append(rec)
    return out


def drop_existing(items: list, prefixes: tuple[str, ...], get_id) -> list:
    return [x for x in items if not any(get_id(x).startswith(p) for p in prefixes)]


def to_lnglat(corners: list[list[float]], size: tuple[float, float]):
    (x0, y0), (x1, y1), (_, _), (x3, y3) = corners
    w, h = size
    return lambda x, y: (x0 + x / w * (x1 - x0) + y / h * (x3 - x0), y0 + x / w * (y1 - y0) + y / h * (y3 - y0))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", choices=["trace", "plan"])
    ap.add_argument("file", type=Path, help="the floor's -trace.json (trace) or plan .geojson (plan)")
    ap.add_argument("points", type=Path, nargs="?", help="JSON list of entries")
    ap.add_argument("--from-detect", type=Path, help="detect_pictograms.py output to take hits from")
    ap.add_argument("--image", help="which image of --from-detect (when it holds several)")
    ap.add_argument("--accept", help="with --from-detect: index:icon,index:icon,...")
    ap.add_argument("--size", help="plan: image / SVG canvas size, WxH in the pixels of the entries")
    ap.add_argument("--level", type=int)
    ap.add_argument("--source", help="provenance source id (e.g. s1) for the new entries")
    ap.add_argument("--replace", action="store_true", help="first drop the existing -pict- / -stairs- entries")
    a = ap.parse_args()

    level = a.level if a.level is not None else level_of(a.file)
    if a.from_detect:
        if not a.accept:
            ap.error("--from-detect needs --accept")
        entries = entries_from_detect(a.from_detect, a.image, a.accept)
    elif a.points:
        entries = json.loads(a.points.read_text(encoding="utf-8"))
    else:
        ap.error("give a points file or --from-detect")
    new = normalise(entries, level, a.source)
    prefixes = (f"L{level}-pict-", f"L{level}-stairs-") if a.replace else ()

    if a.target == "trace":
        old = json.loads(a.file.read_text(encoding="utf-8"))
        clash = {x["id"] for x in old} & {x["id"] for x in new}
        if clash and not a.replace:
            sys.exit(f"ids already in {a.file.name}: {sorted(clash)}; use --replace")
        kept = drop_existing(old, prefixes, lambda x: x["id"])
        a.file.write_text(json.dumps(kept + new, ensure_ascii=False), encoding="utf-8")
        print(f"{len(new)} entries -> {a.file}; next: main.py trace <config> --level {level} --rooms {a.file}")
        return

    if not a.size:
        ap.error("plan needs --size WxH")
    w, h = (float(v) for v in a.size.split("x"))
    fc = json.loads(a.file.read_text(encoding="utf-8"))
    corners = (fc.get("metadata", {}).get("basemap") or {}).get("corners")
    if not corners:
        sys.exit(f"{a.file.name} has no metadata.basemap.corners to place pixels with")
    mapper = to_lnglat(corners, (w, h))
    fc["features"] = drop_existing(fc["features"], prefixes, lambda f: f["properties"]["id"])
    for e in new:
        props = {k: v for k, v in e.items() if k not in ("point", "px")}
        props.setdefault("confidence", "sourced")
        if "point" in e:
            geom = {"type": "Point", "coordinates": list(mapper(*e["point"]))}
        else:
            ring = [list(mapper(x, y)) for x, y in e["px"]]
            geom = {"type": "Polygon", "coordinates": [ring + [ring[0]]]}
        fc["features"].append({"type": "Feature", "properties": props, "geometry": geom})
    a.file.write_text(json.dumps(fc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(new)} features -> {a.file}")


if __name__ == "__main__":
    main()
