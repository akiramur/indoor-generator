from shapely.geometry import box

from indoor_gen import crop

# A 1000 x 500 px image placed at 1 m per pixel with its top-left at (0, 500) m (local y runs up).
CORNERS = [(0, 500), (1000, 500), (1000, 0), (0, 0)]


def test_crop_follows_the_floor_with_a_margin():
    rect = crop.crop_rect(CORNERS, (1000, 500), box(300, 100, 600, 300))
    x0, y0, x1, y1 = rect
    assert x0 < 300 and x1 > 600 and y0 < 200 and y1 > 400   # margin on every side; y is flipped (image y runs down)
    assert x0 >= 300 - 0.05 * 300 - crop.MIN_MARGIN_M - 2 and x1 <= 600 + 0.05 * 300 + crop.MIN_MARGIN_M + 2


def test_crop_skips_images_that_are_already_tight_or_miss_the_floor():
    assert crop.crop_rect(CORNERS, (1000, 500), box(10, 10, 990, 490)) is None
    assert crop.crop_rect(CORNERS, (1000, 500), box(5000, 5000, 5100, 5100)) is None


def test_crop_handles_a_rotated_image():
    # the image is turned 90 degrees against the frame: still a rectangle around the floor in pixels
    rotated = [(0, 0), (0, 1000), (500, 1000), (500, 0)]
    rect = crop.crop_rect(rotated, (1000, 500), box(100, 200, 300, 400))
    assert rect is not None and rect[2] - rect[0] < 1000 and rect[3] - rect[1] < 500
