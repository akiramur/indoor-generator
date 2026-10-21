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
