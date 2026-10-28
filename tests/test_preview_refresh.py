import os

from indoor_gen import build_preview

FC = {"type": "FeatureCollection", "metadata": {"building": "テスト</script>館"}, "features": []}


def _page(tmp_path):
    p = tmp_path / "index.html"
    build_preview.write(FC, p, token="pk.test", images={"a.png": "data:image/png;base64,AAAA"})
    return p


def test_refresh_rebuilds_from_the_template_keeping_the_embedded_data(tmp_path):
    p = _page(tmp_path)
    fresh = p.read_text(encoding="utf-8")
    old = os.stat(build_preview.TEMPLATE).st_mtime - 100
    os.utime(p, (old, old))                      # the page is older than the template
    p.write_text(fresh + "\n<!-- stale -->", encoding="utf-8")
    os.utime(p, (old, old))
    assert build_preview.refresh(p) is True
    assert p.read_text(encoding="utf-8") == fresh
    assert build_preview.refresh(p) is False     # now newer than the template: nothing to do


def test_refresh_leaves_a_page_it_cannot_read_back(tmp_path):
    p = _page(tmp_path)
    p.write_text("<html>an old page</html>", encoding="utf-8")
    old = os.stat(build_preview.TEMPLATE).st_mtime - 100
    os.utime(p, (old, old))
    assert build_preview.refresh(p) is False
    assert p.read_text(encoding="utf-8") == "<html>an old page</html>"


def test_image_url_without_an_extension_is_typed_from_its_bytes(tmp_path, monkeypatch):
    """A CMS image endpoint (`/shop/caimage/photo?id=6279`) has no file extension: the cached file and the data: URL
    get their type from the image itself."""
    import base64

    from indoor_gen import build_preview

    jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 40
    calls = []

    class Resp:
        content = jpg

        def raise_for_status(self):
            pass

    monkeypatch.setattr("requests.get", lambda *a, **k: calls.append(a) or Resp())
    url = "https://example.com/shop/caimage/photo?id=6279&m=960"
    path = build_preview.image_file(url, tmp_path)
    assert path.suffix == ".jpg" and path.read_bytes() == jpg
    assert build_preview.image_file(url, tmp_path) == path and len(calls) == 1   # found in the cache the second time
    fc = {"metadata": {"basemaps": [{"level": 1, "url": url}]}}
    images, warnings = build_preview.basemap_images(fc, tmp_path)
    assert images[url] == "data:image/jpeg;base64," + base64.b64encode(jpg).decode() and not warnings
    assert build_preview.sniff_image_suffix(b"<html>") == "" and build_preview.sniff_image_suffix(b"\x89PNG\r\n") == ".png"
