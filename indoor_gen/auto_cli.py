"""`main.py auto <slug>`: run a building's stages from its recipe, skipping what is up to date (see auto.py)."""

from __future__ import annotations

import sys
from pathlib import Path

from . import auto, recipe


def register(sub, *, root: Path, out_dir, slug_from_arg, write_atomic, print_json) -> None:
    def cmd_auto(args) -> int:
        slug = slug_from_arg(args.target)[0]
        try:
            result = auto.run(root, slug, force=args.force, dry_run=args.dry_run, only=args.only, start=args.start,
                              fetch=args.fetch)
        except recipe.RecipeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print_json(result)
        return 0 if result["ok"] else 1

    p = sub.add_parser("auto", help="run a building's stages (images, steps, build, check) from its recipe.json, skip "
                                    "what is up to date, list what needs a person")
    p.add_argument("target", help="slug (or config path)")
    p.add_argument("--dry-run", action="store_true", help="show what would run; change nothing")
    p.add_argument("--only", metavar="STAGE", help="run just this stage")
    p.add_argument("--start", metavar="STAGE", help="skip the stages before this one")
    p.add_argument("--force", action="store_true", help="run stages even when up to date or edited by hand (overwrites)")
    p.add_argument("--fetch", action="store_true", help="download missing sources for the images stage")
    p.set_defaults(func=cmd_auto)
