"""How well each room is backed by a source: the `report` command (also `output/<slug>/quality.json`).

A room's name (`confidence`) and its shape (`geometry_source`) are separate claims, so there are four grades.
Keep GRADES / grade_of in step with `GRADES` / `gradeOf` in templates/preview.html.
"""

from __future__ import annotations

import json
from pathlib import Path

from indoor_gen.generate_geojson import is_area_room

# traced    - named in a source and the shape comes from one (official data or a drawn floor map)
# listed    - named in a source, but the shape is a share of the floor (ratio)
# estimated - no source
# derived   - what is left between the other rooms (common areas)
GRADES = ("traced", "listed", "estimated", "derived")
WEAK = ("estimated", "listed")


def grade_of(props: dict) -> str:
    if props.get("confidence") == "estimated":
        return "estimated"
    if props.get("geometry_source") == "remainder":
        return "derived"
    if props.get("geometry_source") == "ratio":
        return "listed"
    return "traced"


WEAK_WARN_PCT = 50.0  # `publish` warns about buildings whose weak share (estimated + listed) is above this


def weak_pct(rep: dict) -> float:
    """Share of room area (%) that is estimated or only named in a source (shape guessed)."""
    return round(sum(rep["total"][g]["area_pct"] for g in WEAK), 1)


def publish_warnings(buildings, threshold: float = WEAK_WARN_PCT) -> list[dict]:
    """[{building, weak_pct, message}] for each (slug, feature collection) whose weak share is above `threshold`."""
    out = []
    for slug, fc in buildings:
        pct = weak_pct(report(fc))
        if pct > threshold:
            out.append({"building": slug, "weak_pct": pct,
                        "message": f"{slug}: {pct}% of room area is estimated or has a guessed shape "
                                   f"(threshold {threshold:g}%); see `main.py report {slug}`"})
    return out


def _share(rooms: list[dict]) -> dict:
    total = sum(r["area_m2"] for r in rooms) or 1
    out = {}
    for g in GRADES:
        part = [r for r in rooms if grade_of(r) == g]
        out[g] = {"rooms": len(part), "area_m2": round(sum(r["area_m2"] for r in part), 1),
                  "area_pct": round(100 * sum(r["area_m2"] for r in part) / total, 1)}
    return out


def report(fc: dict) -> dict:
    """Grades per floor and for the whole building, plus the weak rooms (the next ones to research)."""
    rooms = [f["properties"] for f in fc["features"] if is_area_room(f["properties"])]
    floors = []
    for lv in sorted({r["level"] for r in rooms}):
        here = [r for r in rooms if r["level"] == lv]
        floors.append({"level": lv, "floor": here[0]["floor_name"], "grades": _share(here)})
    weak = sorted((r for r in rooms if grade_of(r) in WEAK), key=lambda r: -r["area_m2"])
    return {
        "building": (fc.get("metadata") or {}).get("building"),
        "total": _share(rooms),
        "floors": floors,
        "weak_rooms": [{"id": r["id"], "room_id": r.get("room_id"), "name": r["name"], "floor": r["floor_name"],
                        "grade": grade_of(r), "area_m2": r["area_m2"], "geometry_source": r.get("geometry_source"),
                        "source": r.get("source")} for r in weak],
    }


def shares_from_file(path) -> dict | None:
    """The `total` area shares of an earlier quality.json ({grade: area_pct}), or None."""
    try:
        with open(path, encoding="utf-8") as fh:
            total = json.load(fh)["total"]
        return {g: total[g]["area_pct"] for g in GRADES}
    except (OSError, ValueError, KeyError):
        return None


def change(before: dict | None, rep: dict) -> dict | None:
    """Area share (%) per grade before -> after a rebuild, only for the grades that moved; None if nothing moved
    or there was no earlier report."""
    if before is None:
        return None
    moved = {g: [before[g], rep["total"][g]["area_pct"]] for g in GRADES if before[g] != rep["total"][g]["area_pct"]}
    return moved or None


def format_report(rep: dict, top: int = 10) -> str:
    def line(label, share):
        cells = "  ".join(f"{g} {share[g]['area_pct']:5.1f}% ({share[g]['rooms']})" for g in GRADES)
        return f"{label:<10} {cells}"

    lines = [f"{rep['building'] or ''}  (area share, rooms)", line("total", rep["total"])]
    lines += [line(f["floor"], f["grades"]) for f in rep["floors"]]
    weak = rep["weak_rooms"]
    if weak:
        lines.append(f"\nweakest rooms to research next ({len(weak)} in all, largest first):")
        lines += [f"  {w['floor']:<5} {w['grade']:<9} {w['area_m2']:>7} m²  {w['name']}  [{w['room_id']}]"
                  for w in weak[:top]]
    return "\n".join(lines)


# Room categories of a tenant (a shop, a restaurant, a service counter), as opposed to stairs, corridors, halls.
TENANT_CATEGORIES = ("retail", "restaurant", "service")


def plan_listings(config_path: Path) -> dict[int, dict]:
    """{level: {"listed": n, "excluded": m}} from the plan files of a config: how many tenants the source's own list
    gives for the floor (`metadata.tenants_listed`, written by the importers and `main.py trace --listed`) and how many
    of them were left out on purpose (`tenants_excluded`)."""
    try:
        raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for f in raw.get("floors", []):
        if f.get("plan") is None or f.get("level") is None:
            continue
        try:
            meta = json.loads((Path(config_path).parent / f["plan"]).read_text(encoding="utf-8")).get("metadata") or {}
            level = int(f["level"])
        except (OSError, ValueError):
            continue
        if meta.get("tenants_listed") is not None:
            out[level] = {"listed": meta["tenants_listed"], "excluded": meta.get("tenants_excluded", 0)}
    return out


def listing_check(fc: dict, listings: dict[int, dict]) -> list[dict]:
    """Per floor with a recorded list: the tenants the source lists, how many were left out on purpose, how many
    tenant rooms were built from a source shape (traced) and the difference. A floor that builds fewer shops than the
    source lists is where a shop was missed or merged; more means a zone was cut that the list does not have."""
    rooms = [f["properties"] for f in fc["features"] if is_area_room(f["properties"])]
    rows = []
    for level in sorted(listings):
        here = [r for r in rooms if r["level"] == level]
        if not here:
            continue
        built = sum(1 for r in here if grade_of(r) == "traced" and r.get("category") in TENANT_CATEGORIES)
        expected = listings[level]["listed"] - listings[level]["excluded"]
        diff = built - expected
        verdict = ("matches" if diff == 0 else f"{-diff} fewer built than listed: a shop missed or merged" if diff < 0
                   else f"{diff} more built than listed: a zone the list does not have")
        rows.append({"floor": here[0]["floor_name"], "listed": listings[level]["listed"],
                     "excluded": listings[level]["excluded"], "built": built, "diff": diff, "verdict": verdict})
    return rows


def format_listing(rows: list[dict]) -> str:
    if not rows:
        return "listed vs built: no floor records how many tenants its source lists"
    lines = ["listed vs built (tenants the source lists on a floor, and shops built from a source shape):"]
    for r in rows:
        left = f" ({r['excluded']} left out on purpose)" if r["excluded"] else ""
        lines.append(f"  {r['floor']:>4}  listed {r['listed']:>2}{left}  built {r['built']:>2}  {r['verdict']}")
    return "\n".join(lines)


FIT_OK_PCT = 95.0   # at least this share of a floor's traced area inside the footprint: it fits
FIT_LOW_PCT = 85.0  # below this a lot sticks out


def footprint_check(fc: dict) -> list[dict]:
    """Per floor with shapes taken from a source (traced): how much of their area lies inside the footprint and how
    many rooms are mostly outside it. Traced rooms are never cut to the footprint (the policy), so this only tells how well
    the floor map and the footprint agree: a floor map placed off, or a floor that really extends past the building
    (a basement under the street), shows as area sticking out."""
    from shapely.geometry import shape

    from indoor_gen.geo_utils import LocalFrame

    md = fc.get("metadata") or {}
    frame = LocalFrame(shape(md["footprint"]), md.get("crs", "auto"))
    footprint = frame.to_local(shape(md["footprint"])).buffer(0)
    rows = []
    levels = sorted({f["properties"]["level"] for f in fc["features"]
                     if is_area_room(f["properties"]) and grade_of(f["properties"]) == "traced"})
    for level in levels:
        total = inside = 0.0
        n = n_out = 0
        name = str(level)
        for f in fc["features"]:
            p = f["properties"]
            if p.get("level") != level or not is_area_room(p) or grade_of(p) != "traced":
                continue
            g = frame.to_local(shape(f["geometry"])).buffer(0)
            if g.is_empty or not g.area:
                continue
            name = p.get("floor_name") or name
            i = g.intersection(footprint).area
            total += g.area
            inside += i
            n += 1
            n_out += i / g.area < 0.5
        if not n:
            continue
        pct = 100 * inside / total
        verdict = ("fits" if pct >= FIT_OK_PCT else "a little sticks out" if pct >= FIT_LOW_PCT
                   else "a lot sticks out: the floor map may sit off, or the floor reaches past the building")
        rows.append({"floor": name, "traced_rooms": n, "traced_area_m2": round(total), "inside_pct": round(pct, 1),
                     "rooms_mostly_outside": int(n_out), "verdict": verdict})
    return rows


def format_footprint(rows: list[dict]) -> str:
    if not rows:
        return "footprint overlap: no floor has shapes taken from a source"
    lines = ["footprint overlap (share of each floor's traced area inside the footprint; rooms are never cut to it):"]
    for r in rows:
        out = f", {r['rooms_mostly_outside']} room(s) mostly outside" if r["rooms_mostly_outside"] else ""
        lines.append(f"  {r['floor']:>4}  {r['traced_rooms']:>3} rooms {r['traced_area_m2']:>5} m²  inside {r['inside_pct']:>5.1f}%{out}  {r['verdict']}")
    return "\n".join(lines)


def placement_basis(cfg: dict) -> list[dict]:
    """What each floor map's position rests on: `landmarks` (a similarity fitted to point pairs, with its residual) or `outline`
    (only scaled / turned / moved to cover the footprint: it inherits the footprint's error: several metres, more on a
    large building). A floor whose basemap has no `alignment` counts as outline."""
    rows = []
    for fl in cfg.get("floors", []):
        bm = fl.get("basemap")
        if not bm:
            continue
        al = bm.get("alignment")
        name = str(fl.get("name") or fl.get("level"))
        if al:
            rows.append({"floor": name, "basis": "landmarks", "pairs": len(al.get("pairs", [])), "rms_m": al.get("rms_m")})
        else:
            rows.append({"floor": name, "basis": "outline", "pairs": 0, "rms_m": None})
    return rows


def format_placement(rows: list[dict]) -> str:
    if not rows:
        return "Floor-map placement: no floor map."
    lines = ["Floor-map placement (what the position rests on):"]
    for r in rows:
        what = (f"{r['pairs']} landmark pairs, rms {r['rms_m']} m" if r["basis"] == "landmarks"
                else "the footprint outline only (no landmark alignment): may be several metres off")
        lines.append(f"  {r['floor']}: {what}")
    return "\n".join(lines)
