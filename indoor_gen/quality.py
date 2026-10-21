"""How well each room is backed by a source: the `report` command (also `output/<slug>/quality.json`).

A room's name (`confidence`) and its shape (`geometry_source`) are separate claims, so there are four grades.
Keep GRADES / grade_of in step with `GRADES` / `gradeOf` in templates/preview.html.
"""

from __future__ import annotations

import json

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
