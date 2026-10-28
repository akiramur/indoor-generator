"""`main.py recipe <slug>`: make a building's third-party floor-map images again from its `recipe.json` (see recipe.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import recipe


def register(sub, *, root: Path, out_dir, slug_from_arg, write_atomic, print_json) -> None:
    def cmd_recipe(args) -> int:
        slug = slug_from_arg(args.target)[0]
        try:
            rec = recipe.load(root, slug)
            fetched = {}
            for src in rec.get("sources", []):
                if args.fetch:
                    fetched[src["id"]] = recipe.fetch(root, slug, src)
                elif not recipe.source_file(root, slug, src).exists():
                    fetched[src["id"]] = "missing"
            if args.record:
                recipe.record_sources(root, slug, rec)
            rows = recipe.make_images(root, slug, rec, record=args.record)
        except recipe.RecipeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        except Exception as e:  # a PDF renderer or an image that does not open: say which, not a traceback
            print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        if args.record:
            write_atomic(recipe.recipe_path(root, slug), json.dumps(rec, ensure_ascii=False, indent=2) + "\n")
        print_json({"slug": slug, "sources": fetched, "images": rows, "recorded": args.record,
                    "steps": [s.get("name") for s in rec.get("steps", [])]})
        return 1 if any(r["status"] == "changed" for r in rows) else 0

    p = sub.add_parser("recipe", help="make a building's floor-map images (maps/*.png, git-ignored) again from "
                                      "configs/plans/<slug>/recipe.json and the cached sources")
    p.add_argument("target", help="slug (or config path)")
    p.add_argument("--fetch", action="store_true", help="download sources that are not cached yet")
    p.add_argument("--record", action="store_true",
                   help="authoring: write the sha256 of the sources and of the made images into the recipe")
    p.set_defaults(func=cmd_recipe)
