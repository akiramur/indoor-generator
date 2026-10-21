import numpy as np
from PIL import Image

from indoor_gen.importers import imagemap_floorguide as im

PAGE = """
<img src="/img/floor.png" width="300" height="200" usemap="#Map" />
<map name="Map">
  <area shape="rect" coords="10,20,50,60" href="/shop/a/" alt="Shop A">
  <area shape="poly" coords="100,100,140,100,140,150,100,150" href="/shop/b/" alt="Shop B">
  <area shape="circle" coords="200,50,10" href="/shop/c/">
</map>
"""


def test_area_geometry_shapes():
    assert im.area_geometry("rect", "50,60,10,20").bounds == (10, 20, 50, 60)
    assert im.area_geometry("poly", "0,0,10,0,10,10,0,10").area == 100
    assert abs(im.area_geometry("circle", "0,0,10").area - 314.16) < 2
    assert im.area_geometry("poly", "0,0,1,1") is None


def test_parser_finds_image_and_areas():
    p = im._ImageMapParser()
    p.feed(PAGE)
    assert p.images[0]["src"] == "/img/floor.png"
    areas = p.maps["Map"]
    assert [a.get("alt") for a in areas] == ["Shop A", "Shop B", None]


def test_image_outline_drops_detached_labels_and_scales_to_display(tmp_path):
    a = np.zeros((100, 200, 4), dtype=np.uint8)  # transparent background
    a[10:90, 20:120] = (214, 215, 215, 255)       # building
    a[40:50, 150:190] = (0, 0, 0, 255)            # label outside the building
    path = tmp_path / "f.png"
    Image.fromarray(a).save(path)
    outline, canvas = im.image_outline(path, (400, 200))  # shown at 2x
    assert canvas == (400, 200)
    x0, y0, x1, y1 = outline.bounds
    assert (round(x0), round(y0), round(x1), round(y1)) == (40, 20, 240, 180)
