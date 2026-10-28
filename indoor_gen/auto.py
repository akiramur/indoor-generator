"""`main.py auto <slug>`: run a building's stages in order, skip what is up to date, and list what needs a person.

The stages come from the building's recipe (`configs/plans/<slug>/recipe.json`, see recipe.py):
  images   the floor-map images of the recipe
  <steps>  each recipe step with a `cmd` (run through this Python from the repository root) is a stage; a step without
           one is manual (its `review` text, or its `run` prose, goes to the review list until its `outputs` exist; `optional` keeps it off the list)
  build    `main.py build configs/<slug>.json`
  check    never runs a command: reads the build (sourcing, share of traced area inside the footprint, what each floor
           map's position rests on) and says what to look at

A step may declare `inputs` and `outputs` (paths from the repository root). State is kept in `output/<slug>/auto/state.json`:
a stage's key is the hash of its command and its inputs. A stage whose key is unchanged is skipped; one that changed runs
again, unless its output was edited by hand since the last run (its hash differs from the recorded one), then it is left alone
and reported (`--force` overwrites). A stage that was never run by `auto` but whose outputs exist is adopted as done, not run:
the tools overwrite their output, which would lose pictograms added later and hand edits."""

from __future__ import annotations

import datetime
import hashlib
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable

from . import recipe as recipe_mod

PLACEMENT_REVIEW = "position rests on the OSM outline only (no landmark alignment): it inherits the footprint's error, which can be several metres or more on a large building"
WEAK_PCT = 50.0  # same default as `publish --warn-weak-pct`


def state_path(root: Path, slug: str) -> Path:
    return root / "output" / slug / "auto" / "state.json"


def load_state(root: Path, slug: str) -> dict:
    try:
        return json.loads(state_path(root, slug).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"stages": {}}


def hash_paths(root: Path, paths: list[str]) -> str | None:
    """One hash over the files (a directory: every file under it); None when one of them is missing."""
    h = hashlib.sha256()
    for rel in sorted(paths):
        p = root / rel
        files = sorted(q for q in p.rglob("*") if q.is_file()) if p.is_dir() else [p]
        if not files or not all(f.is_file() for f in files):
            return None
        for f in files:
            h.update(str(f.relative_to(root)).encode())
            h.update(recipe_mod.sha256_file(f).encode())
    return h.hexdigest()


def stage_list(root: Path, slug: str, rec: dict) -> list[dict]:
    plans = f"configs/plans/{slug}"
    stages = [{"name": "images", "kind": "images"}]
    outs = []
    for i, step in enumerate(rec.get("steps", [])):
        st = {"name": step.get("name") or f"step-{i + 1}", "kind": "cmd" if step.get("cmd") else "manual",
              "cmd": step.get("cmd"), "inputs": step.get("inputs", []), "outputs": step.get("outputs", []),
              "review": step.get("review") or step.get("run") or "", "optional": bool(step.get("optional"))}
        stages.append(st)
        outs += st["outputs"]
    stages.append({"name": "build", "kind": "cmd", "cmd": f"main.py build configs/{slug}.json",
                   "inputs": [f"configs/{slug}.json", plans] + outs, "outputs": [], "review": ""})
    stages.append({"name": "check", "kind": "check"})
    return stages


def run_command(root: Path, cmd: str) -> tuple[int, str]:
    r = subprocess.run([sys.executable, *shlex.split(cmd)], cwd=root, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()[-600:]


def run(root: Path, slug: str, *, runner: Callable[[Path, str], tuple[int, str]] = run_command, force: bool = False,
        dry_run: bool = False, only: str | None = None, start: str | None = None, fetch: bool = False) -> dict:
    rec = recipe_mod.load(root, slug)
    state = load_state(root, slug)
    recorded = state.setdefault("stages", {})
    rows, review, failed = [], [], False
    skipping = start is not None
    for st in stage_list(root, slug, rec):
        name = st["name"]
        if (only and name != only) or (skipping and name != start):
            rows.append({"stage": name, "status": "not-selected"})
            continue
        skipping = False
        row = {"stage": name}
        if failed and st["kind"] != "check":
            row["status"] = "not-run (an earlier stage failed)"
        elif st["kind"] == "images":
            row.update(images_stage(root, slug, rec, fetch=fetch, dry_run=dry_run))
        elif st["kind"] == "check":
            row.update(check_stage(root, slug))
            review += [f"check: {m}" for m in row.get("review", [])]
        elif st["kind"] == "manual":
            done = bool(st["outputs"]) and hash_paths(root, st["outputs"]) is not None
            row["status"] = "manual (outputs exist)" if done else "manual"
            if not done and not st.get("optional"):
                review.append(f"{name}: {st['review'] or 'do by hand'}")
        else:
            row.update(command_stage(root, st, recorded.get(name), runner, force=force, dry_run=dry_run))
            if row["status"] == "failed":
                failed = True
            if row["status"].startswith("edited by hand"):
                review.append(f"{name}: output edited by hand since the last run; not overwritten (--force)")
            if not dry_run and row.get("record") is not None:
                recorded[name] = row.pop("record")
        row.pop("record", None)
        rows.append(row)
    if not dry_run:
        p = state_path(root, slug)
        p.parent.mkdir(parents=True, exist_ok=True)
        state["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
        p.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"slug": slug, "stages": rows, "review": review, "ok": not failed}


def images_stage(root: Path, slug: str, rec: dict, *, fetch: bool, dry_run: bool) -> dict:
    maps = root / "configs" / "plans" / slug / "maps"
    if not rec.get("images"):
        return {"status": "nothing to do"}
    if all((maps / i["out"]).exists() for i in rec["images"]):
        return {"status": "up to date"}
    if dry_run:
        return {"status": "would make the images"}
    try:
        for src in rec.get("sources", []):
            if fetch:
                recipe_mod.fetch(root, slug, src)
        rows = recipe_mod.make_images(root, slug, rec)
    except recipe_mod.RecipeError as e:
        return {"status": "failed", "message": str(e)}
    changed = [r["out"] for r in rows if r["status"] == "changed"]
    return {"status": "failed" if changed else "made", "message": f"pixels differ from the recipe: {changed}" if changed else ""}


def command_stage(root: Path, st: dict, prev: dict | None, runner, *, force: bool, dry_run: bool) -> dict:
    ins = hash_paths(root, st["inputs"]) if st["inputs"] else ""
    key = hashlib.sha256(f"{st['cmd']}\n{ins}".encode()).hexdigest()
    out_now = hash_paths(root, st["outputs"]) if st["outputs"] else None
    if prev is None and st["outputs"] and out_now is not None and not force:
        return {"status": "adopted (outputs exist, not run)", "record": {"key": key, "outputs": out_now}}
    if prev and prev.get("key") == key and not force:
        return {"status": "up to date"}
    if prev and st["outputs"] and prev.get("outputs") not in (None, out_now) and not force:
        return {"status": "edited by hand: not overwritten"}
    if dry_run:
        return {"status": "would run", "cmd": st["cmd"]}
    code, tail = runner(root, st["cmd"])
    if code:
        return {"status": "failed", "message": tail}
    return {"status": "ran", "record": {"key": key, "outputs": hash_paths(root, st["outputs"]) if st["outputs"] else None,
                                         "at": datetime.datetime.now().isoformat(timespec="seconds")}}


def check_stage(root: Path, slug: str) -> dict:
    """What to look at after a build: weak sourcing, traced area outside the footprint, floor maps placed by outline only."""
    from . import generate_geojson, quality

    review: list[str] = []
    fc = generate_geojson.feature_collection_from_file(root / "output" / slug / "indoor.geojson")
    if fc is None:
        return {"status": "no build yet", "review": ["no output/<slug>/indoor.geojson: build first"]}
    floors = []
    q = json.loads((root / "output" / slug / "quality.json").read_text(encoding="utf-8")) if (
        root / "output" / slug / "quality.json").exists() else {}
    total = (q.get("total") or {})
    weak = sum((total.get(g) or {}).get("area_pct", 0) for g in ("estimated", "derived"))
    if weak > WEAK_PCT:
        review.append(f"{weak:.0f}% of the area is estimated or derived (sourced shapes cover less than half)")
    for row in quality.footprint_check(fc):
        floors.append(row)
        if row["inside_pct"] < quality.FIT_OK_PCT:
            review.append(f"{row['floor']}: {row['inside_pct']}% of the traced area is inside the footprint, "
                          f"{row['rooms_mostly_outside']} rooms mostly outside ({row['verdict']})")
    placement = {}
    try:
        cfg = json.loads((root / "configs" / f"{slug}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cfg = {"floors": []}
    floors_by_name = {str(f.get("name") or f.get("level")): f for f in cfg.get("floors", [])}
    for row in quality.placement_basis(cfg):
        name = row["floor"]
        if row["basis"] == "landmarks":
            placement[name] = f"landmarks: {row['pairs']} pairs, rms {row['rms_m']} m"
        else:
            placement[name] = "outline fit only"
            review.append(f"{name}: {PLACEMENT_REVIEW}")
        fl = floors_by_name[name]
        turn = orientation_of(root, slug, fl, fl["basemap"])
        if turn:
            v = turn["verdict"]
            placement[name] += f"; orientation: {v['status']} ({v['why']})"
            if v["status"] != "clear" or v["best_turn_deg"]:
                review.append(f"{name}: which way the map is turned is not settled by names "
                              f"({v['status']}, best turn {v['best_turn_deg']}°: {v['why']})")
    return {"status": "checked", "sourcing_pct": {g: (total.get(g) or {}).get("area_pct", 0) for g in total},
            "footprint": floors, "placement": placement, "review": review}


def orientation_of(root: Path, slug: str, floor: dict, basemap: dict) -> dict | None:
    """The floor map's turn checked against OSM names, from files already on disk (`main.py orient` reads OSM and caches
    `output/<slug>/osm_names.json`); None when the cache, the trace or a local image is missing."""
    from PIL import Image

    from . import orient

    names = root / "output" / slug / "osm_names.json"
    name = floor.get("name") or str(floor.get("level"))
    trace = root / "configs" / "plans" / slug / f"{name}-trace.json"
    image = basemap.get("image", "")
    img = root / "configs" / image
    if not (names.exists() and trace.exists() and basemap.get("corners")) or image.startswith("http") or not img.exists():
        return None
    try:
        places = json.loads(names.read_text(encoding="utf-8"))
        named = orient.zone_centres(json.loads(trace.read_text(encoding="utf-8")))
        with Image.open(img) as im:
            size = (im.width, im.height)
    except (OSError, json.JSONDecodeError):
        return None
    if not named:
        return None
    rows = orient.score_turns(basemap["corners"], size, named, places)
    return {"turns": rows, "verdict": orient.verdict(rows)}
