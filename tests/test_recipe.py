"""`main.py recipe`: floor-map images made again from a recipe (local sources, no network)."""
import json

import pytest
from PIL import Image

from indoor_gen import recipe


def make_root(tmp_path):
    slug = "b"
    src = tmp_path / "output" / slug / "sources"
    src.mkdir(parents=True)
    im = Image.new("RGB", (8, 6), (255, 255, 255))
    im.putpixel((1, 1), (255, 0, 0))
    im.save(src / "map.png")
    rec = {"sources": [{"id": "m", "file": "map.png"}],
           "images": [{"out": "F1.png", "from": "m", "ops": [{"op": "crop", "box": [0, 0, 4, 3]},
                                                            {"op": "resize", "factor": 2, "resample": "nearest"}]}]}
    (tmp_path / "configs" / "plans" / slug).mkdir(parents=True)
    recipe.recipe_path(tmp_path, slug).write_text(json.dumps(rec), encoding="utf-8")
    return slug


def test_images_are_made_recorded_and_then_checked_against_their_hash(tmp_path):
    slug = make_root(tmp_path)
    rec = recipe.load(tmp_path, slug)
    rows = recipe.make_images(tmp_path, slug, rec)
    assert rows[0]["status"] == "no-hash" and rows[0]["size"] == [8, 6]
    made = Image.open(tmp_path / "configs" / "plans" / slug / "maps" / "F1.png")
    assert made.getpixel((2, 2)) == (255, 0, 0) and made.getpixel((3, 3)) == (255, 0, 0)  # NEAREST: one pixel -> 2x2
    recipe.record_sources(tmp_path, slug, rec)
    recipe.make_images(tmp_path, slug, rec, record=True)
    assert recipe.make_images(tmp_path, slug, rec)[0]["status"] == "ok"
    rec["images"][0]["ops"][0]["box"] = [0, 0, 5, 3]
    assert recipe.make_images(tmp_path, slug, rec)[0]["status"] == "changed"


def test_a_source_that_changed_or_is_missing_is_an_error(tmp_path):
    slug = make_root(tmp_path)
    rec = recipe.load(tmp_path, slug)
    rec["sources"][0]["sha256"] = "0" * 64
    with pytest.raises(recipe.RecipeError, match="differs"):
        recipe.make_images(tmp_path, slug, rec)
    (tmp_path / "output" / slug / "sources" / "map.png").unlink()
    with pytest.raises(recipe.RecipeError, match="not cached"):
        recipe.make_images(tmp_path, slug, rec)
    with pytest.raises(recipe.RecipeError, match="no url"):
        recipe.fetch(tmp_path, slug, rec["sources"][0])


def test_unknown_source_in_an_image_is_refused(tmp_path):
    slug = make_root(tmp_path)
    p = recipe.recipe_path(tmp_path, slug)
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["images"][0]["from"] = "nope"
    p.write_text(json.dumps(rec), encoding="utf-8")
    with pytest.raises(recipe.RecipeError, match="naming a source id"):
        recipe.load(tmp_path, slug)
