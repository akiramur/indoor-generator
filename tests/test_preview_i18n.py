import json
import re

from indoor_gen import build_preview
from indoor_gen.config import CATEGORY_COLORS

JA = re.compile(r"[\u3000-\u30ff\u4e00-\u9fff\uff00-\uffef]")
I18N = json.loads(build_preview.I18N.read_text(encoding="utf-8"))
TEMPLATE = build_preview.TEMPLATE.read_text(encoding="utf-8")
BODY = TEMPLATE.split("<body>", 1)[1].split("<script", 1)[0]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _tl_keys() -> set[str]:
    return {m.group(1).replace("\\'", "'") for m in re.finditer(r"\btl\('((?:[^'\\]|\\.)*)'", TEMPLATE)}


def _static_texts() -> set[str]:
    """English text nodes and title / aria-label / placeholder values of the markup, as translateStatic reads them."""
    texts = {_norm(t) for t in re.findall(r">([^<>]+)<", BODY)}
    texts |= {_norm(v) for v in re.findall(r'(?:title|aria-label|placeholder)="([^"]*)"', BODY)}
    return texts


def test_the_page_is_written_in_english():
    """Japanese lives in i18n.json; the template keeps it only in comments and the language button."""
    left = []
    for line in TEMPLATE.split("\n"):
        line = re.sub(r"<!--.*?-->", "", re.sub(r"//.*$", "", line))
        if JA.search(line) and "'日本語'" not in line:
            left.append(line.strip())
    assert left == []


def test_every_tl_text_and_static_text_is_a_key_or_unused_key_is_flagged():
    keys = set(I18N["ja"])
    assert _tl_keys() <= keys                                   # a text with no Japanese would show English in Japanese mode
    unused = sorted(keys - _tl_keys() - _static_texts())
    assert unused == []                                          # a stale entry: the text was removed or reworded


def test_japanese_texts_are_japanese_and_placeholders_match():
    for en, ja in I18N["ja"].items():
        assert JA.search(ja), en
        assert sorted(re.findall(r"\{\w+\}", en)) == sorted(re.findall(r"\{\w+\}", ja)), en


def test_every_category_has_a_label_in_both_languages():
    for cat in [*CATEGORY_COLORS, "unassigned", "floor", "hotel", "seating", "cafe", "hospital", "pharmacy"]:
        assert set(I18N["categories"][cat]) == {"ja", "en"}, cat


def test_rendered_page_embeds_the_dictionary():
    page = build_preview.render({"type": "FeatureCollection", "metadata": {}, "features": []})
    assert "__I18N__" not in page and "共用部・通路" in page
