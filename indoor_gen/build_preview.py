"""Render the Mapbox GL JS preview page for a generated FeatureCollection."""

from __future__ import annotations

import html
import json
from pathlib import Path

from .config import CATEGORY_COLORS
from .publish import POI_BADGES

TEMPLATE = Path(__file__).parent / "templates" / "preview.html"
I18N = TEMPLATE.with_name("i18n.json")   # English texts (keyed by the page's Japanese), category labels, generated room names


def _i18n() -> str:
    return _script_json(json.loads(I18N.read_text(encoding="utf-8")))


def _script_json(obj) -> str:
    # Keep embedded JSON from closing the <script> element.
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def render(fc: dict, token: str | None = None, images: dict[str, str] | None = None) -> str:
    """`images` maps basemap URLs to data: URLs embedded in the page."""
    title = html.escape(fc.get("metadata", {}).get("building") or "Indoor map")
    return (TEMPLATE.read_text(encoding="utf-8")
            .replace("__TITLE__", title)
            .replace("__TOKEN__", _script_json(token or ""))
            .replace("__IMAGES__", _script_json(images or {}))
            .replace("__POI_BADGES__", _script_json(POI_BADGES))
            .replace("__CATEGORIES__", _script_json(CATEGORY_COLORS))
            .replace("__I18N__", _i18n())
            .replace("__DATA__", _script_json(fc)))


def write(fc: dict, out_path: Path, token: str | None = None, images: dict[str, str] | None = None) -> Path:
    out_path.write_text(render(fc, token, images), encoding="utf-8")
    return out_path


def refresh(index_path: Path) -> bool:
    """Re-render an already built page from the current template, keeping its embedded data, images, token and title
    (read back from the page itself), so a template change reaches every building without rebuilding it.
    Only when the template is newer than the page; False when nothing was done (up to date, no page, or the page was
    made from a template whose lines differ too much to read them back: it then stays as it is until the next build)."""
    import os
    import re

    if not index_path.exists() or max(TEMPLATE.stat().st_mtime, I18N.stat().st_mtime) <= index_path.stat().st_mtime:
        return False
    old = index_path.read_text(encoding="utf-8").split("\n")
    new = TEMPLATE.read_text(encoding="utf-8")
    values: dict[str, str] = {}
    for line in new.split("\n"):
        for ph in ("__TITLE__", "__DATA__", "__IMAGES__", "__TOKEN__"):
            if ph not in line:
                continue
            prefix, suffix = line.split(ph, 1)
            suffix = suffix.rstrip()
            hit = next((o.rstrip() for o in old if o.startswith(prefix) and o.rstrip().endswith(suffix)
                        and len(o.rstrip()) >= len(prefix) + len(suffix)), None)
            if hit is None:
                return False
            values[ph] = hit[len(prefix):len(hit) - len(suffix)] if suffix else hit[len(prefix):]
    if set(values) != {"__TITLE__", "__DATA__", "__IMAGES__", "__TOKEN__"}:
        return False
    for ph, v in values.items():
        new = new.replace(ph, v)
    new = (new.replace("__POI_BADGES__", _script_json(POI_BADGES)).replace("__CATEGORIES__", _script_json(CATEGORY_COLORS))
           .replace("__I18N__", _i18n()))
    if re.search(r"\b__[A-Z_]+__\b", new):   # a placeholder this code does not know: leave the page for the next build
        return False
    tmp = index_path.with_suffix(".html.tmp")
    tmp.write_text(new, encoding="utf-8")
    os.replace(tmp, index_path)
    return True


MIME = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def sniff_image_suffix(data: bytes) -> str:
    """File suffix for image bytes ('' when unknown): for a URL with no extension, like a CMS endpoint (`/photo?id=6279`)."""
    head = data[:16]
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".svg" if b"<svg" in data[:2048] else ""


def image_file(url: str, cache_dir: Path, base_dir: Path | None = None) -> Path:
    """Local file for a basemap image. http(s) URLs are downloaded once into `cache_dir`
    (under output/, so third-party maps stay out of the repository); other values are
    paths relative to `base_dir` (the config's folder)."""
    import hashlib

    import requests

    if not url.startswith(("http://", "https://")):
        return (base_dir or Path.cwd()) / url
    suffix = Path(url.split("?")[0]).suffix.lower()
    stem = hashlib.sha1(url.encode()).hexdigest()[:16]
    if not suffix:  # no extension in the URL: the cached file's suffix comes from the image itself
        cached = sorted(cache_dir.glob(stem + ".*")) if cache_dir.exists() else []
        if cached:
            return cached[0]
    path = cache_dir / (stem + suffix)
    if not path.exists():
        resp = requests.get(url, headers={"User-Agent": "indoor-generator"}, timeout=60)
        resp.raise_for_status()
        if not suffix:
            suffix = sniff_image_suffix(resp.content)
            if not suffix:
                raise ValueError(f"{url} is not an image this tool reads ({', '.join(MIME)})")
            path = cache_dir / (stem + suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
    return path


def basemap_images(fc: dict, cache_dir: Path, base_dir: Path | None = None,
                   files: dict[str, Path] | None = None) -> tuple[dict[str, str], list[str]]:
    """Fetch (or reuse cached) basemap images as data: URLs. Returns (images, warnings).

    Both the shown image (`url`) and, for cropped basemaps, the original (`source_url`, used
    by the alignment pane) are embedded. `files` maps generated urls to their files."""
    import base64

    import requests

    images, warnings = {}, []
    for bm, url in [(bm, u) for bm in fc.get("metadata", {}).get("basemaps", [])
                    for u in (bm["url"], bm.get("source_url")) if u]:
        try:
            path = (files or {}).get(url) or image_file(url, cache_dir, base_dir)
            suffix = path.suffix.lower()  # of the cached file: right even when the URL has no extension
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            images[url] = f"data:{MIME.get(suffix, 'application/octet-stream')};base64,{data}"
        except (requests.RequestException, OSError, ValueError) as e:
            warnings.append(f"basemap for level {bm['level']} not available: {e}")
    return images, warnings
