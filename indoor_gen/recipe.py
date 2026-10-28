"""A building's recipe (`configs/plans/<slug>/recipe.json`): where its third-party floor-map images come from and how they
were cut, so `maps/*.png` (git-ignored) can be made again, byte for byte in pixels, from the cached sources.

    {"sources": [{"id": "pdf", "url": "...", "file": "kannai.pdf", "sha256": "...", "note": "..."}],
     "images":  [{"out": "4F.png", "from": "pdf", "ops": [{"op": "pdf_page", "page": 2, "dpi": 200},
                                                          {"op": "crop", "box": [x0, y0, x1, y1]}],
                  "pixels_sha256": "..."}],
     "steps":   [{"name": "shops 4F", "run": "tools/color_zones.py ..."}]}

Sources are cached under `output/<slug>/sources/` (never committed); images are written to `configs/plans/<slug>/maps/`.
`steps` document the commands that made the plan data; `main.py recipe` does not run them, `main.py auto` runs the ones with a `cmd` (see auto.py). `pixels_sha256` is over the image's size and
RGB bytes (not the PNG file, whose encoding may differ between Pillow versions)."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from PIL import Image


class RecipeError(Exception):
    pass


def recipe_path(root: Path, slug: str) -> Path:
    return root / "configs" / "plans" / slug / "recipe.json"


def load(root: Path, slug: str) -> dict:
    path = recipe_path(root, slug)
    try:
        recipe = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise RecipeError(f"no recipe for {slug}: {path} does not exist")
    except json.JSONDecodeError as e:
        raise RecipeError(f"{path}: {e}")
    ids = [s.get("id") for s in recipe.get("sources", [])]
    if len(set(ids)) != len(ids) or None in ids:
        raise RecipeError("every source needs its own id")
    for img in recipe.get("images", []):
        if not img.get("out") or img.get("from") not in ids:
            raise RecipeError(f"image {img.get('out')!r}: `out` and a `from` naming a source id are required")
    return recipe


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pixels_sha256(im: Image.Image) -> str:
    im = im.convert("RGB")
    return hashlib.sha256(f"{im.width}x{im.height}".encode() + im.tobytes()).hexdigest()


def source_file(root: Path, slug: str, src: dict) -> Path:
    return root / "output" / slug / "sources" / src["file"]


def fetch(root: Path, slug: str, src: dict) -> str:
    """Download a missing source. Returns "have" / "fetched"; no header tricks: if the site refuses, say so."""
    dest = source_file(root, slug, src)
    if dest.exists():
        return "have"
    if not src.get("url"):
        raise RecipeError(f"source {src['id']}: {dest} is missing and the recipe has no url")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(src["url"], timeout=60) as r, dest.open("wb") as f:
            shutil.copyfileobj(r, f)
    except OSError as e:
        dest.unlink(missing_ok=True)
        raise RecipeError(f"source {src['id']}: could not download {src['url']} ({e}); save it by hand as {dest}")
    return "fetched"


def apply_ops(path: Path, ops: list[dict]) -> Image.Image:
    """The source file through `ops` in order. A PDF source must start with `pdf_page`; an image source is opened as is."""
    im: Image.Image | None = None
    for op in ops:
        kind = op.get("op")
        if kind == "pdf_page":
            if im is not None:
                raise RecipeError("pdf_page must come first")
            im = render_pdf_page(path, int(op["page"]), int(op.get("dpi", 200)))
        else:
            if im is None:
                im = Image.open(path)
            if kind == "crop":
                im = im.crop(tuple(int(v) for v in op["box"]))
            elif kind == "resize":
                factor = int(op["factor"])
                resample = {"nearest": Image.NEAREST, "lanczos": Image.LANCZOS}[op.get("resample", "nearest")]
                im = im.resize((im.width * factor, im.height * factor), resample)
            else:
                raise RecipeError(f"unknown op {kind!r}")
    if im is None:
        im = Image.open(path)
    return im.convert("RGB")


def render_pdf_page(path: Path, page: int, dpi: int) -> Image.Image:
    exe = shutil.which("pdftoppm")
    if exe is None:
        raise RecipeError("pdftoppm (poppler) is needed to render a PDF page")
    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "p"
        subprocess.run([exe, "-r", str(dpi), "-png", "-f", str(page), "-l", str(page), str(path), str(prefix)],
                       check=True, capture_output=True)
        out = next(Path(tmp).glob("p-*.png"))
        return Image.open(out).convert("RGB")


def make_images(root: Path, slug: str, recipe: dict, *, record: bool = False) -> list[dict]:
    """Make every image of the recipe into `configs/plans/<slug>/maps/`. Each row: out, status (ok / changed / recorded /
    no-hash), pixels_sha256. `record` writes the hashes of what was made into the returned recipe's images (the caller saves)."""
    sources = {s["id"]: s for s in recipe.get("sources", [])}
    rows = []
    for img in recipe.get("images", []):
        src = sources[img["from"]]
        path = source_file(root, slug, src)
        if not path.exists():
            raise RecipeError(f"{img['out']}: source {src['file']} is not cached ({path}); run with --fetch")
        if src.get("sha256") and sha256_file(path) != src["sha256"]:
            raise RecipeError(f"source {src['file']} differs from the recipe (sha256); the site may have changed it")
        im = apply_ops(path, img.get("ops", []))
        digest = pixels_sha256(im)
        dest = root / "configs" / "plans" / slug / "maps" / img["out"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        im.save(dest)
        want = img.get("pixels_sha256")
        if record:
            img["pixels_sha256"], status = digest, "recorded"
        else:
            status = "no-hash" if not want else ("ok" if want == digest else "changed")
        rows.append({"out": img["out"], "status": status, "pixels_sha256": digest, "size": [im.width, im.height]})
    return rows


def record_sources(root: Path, slug: str, recipe: dict) -> None:
    for src in recipe.get("sources", []):
        path = source_file(root, slug, src)
        if path.exists():
            src["sha256"] = sha256_file(path)
