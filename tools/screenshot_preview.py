"""Screenshot the live preview (`main.py serve`) in headless Chrome, to check a build by eye.

Playwright is not in requirements.txt (optional): `.venv/bin/pip install playwright`. It drives an installed Google Chrome
(`--channel chrome`, the default); otherwise run `.venv/bin/playwright install chromium` and pass `--channel chromium`.
The preview must be running (`main.py serve <slug>`; one server holds every building, the picker switches between them).

    .venv/bin/python tools/screenshot_preview.py --out /tmp/shot.png \\
        [--slug marubiru-sample]       building to show (default: whatever the page opens with)
        [--floor 2]                        floor button of the IndoorControl (bottom right: "1", "2", "B1", ...)
        [--center 139.7638,35.6810] [--zoom 18.9] [--pitch 0] [--bearing 0]   camera (zoom/pitch/bearing keep the
                                           current value when omitted; use --center to jump to a spot)
        [--set show-map=off --set show-outline=on]   the 表示 section's checkboxes: show-outline, show-map, show-grade
        [--opacity 40]                     room opacity slider, 0-100
        [--size 1400x900] [--url http://localhost:8765/] [--wait 7]

The map takes a few seconds (software rendering, 3D buildings): --wait is the settle time in seconds after the page opens.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


async def run(a: argparse.Namespace) -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        sys.exit("playwright is not installed: .venv/bin/pip install playwright")
    w, h = (int(v) for v in a.size.split("x"))
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel=None if a.channel == "chromium" else a.channel, headless=True,
                                          args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        page = await browser.new_page(viewport={"width": w, "height": h})
        page.on("pageerror", lambda e: print("page error:", str(e)[:300], file=sys.stderr))
        try:
            await page.goto(a.url)
        except Exception as e:  # noqa: BLE001 - connection refused etc.
            sys.exit(f"cannot open {a.url}: {e}\nstart the preview first: .venv/bin/python main.py serve <slug>")
        await page.wait_for_timeout(a.wait * 1000)
        if a.slug:
            await page.select_option("#building-picker", a.slug)
            await page.wait_for_timeout(a.wait * 1000 + 2000)
        for spec in a.set:
            name, value = spec.split("=")
            # the checkboxes sit in the left pane's 表示 section (it can be folded): set them directly
            await page.evaluate("([id, on]) => { const e = document.getElementById(id); e.checked = on; e.dispatchEvent(new Event('change')); }",
                                [name, value.lower() in ("on", "1", "true")])
        if a.opacity is not None:
            await page.evaluate("v => { const e = document.querySelector('#rooms-opacity'); e.value = v; "
                                "e.dispatchEvent(new Event('input')); }", a.opacity)
        cam = {k: v for k, v in (("zoom", a.zoom), ("pitch", a.pitch), ("bearing", a.bearing)) if v is not None}
        if a.center:
            lng, lat = (float(v) for v in a.center.split(","))
            cam["center"] = [lng, lat]
        if cam:
            await page.evaluate("c => map.jumpTo(c)", cam)
            await page.wait_for_timeout(4000)
        if a.floor:
            await page.locator(".mapboxgl-ctrl-bottom-right").get_by_text(a.floor, exact=True).first.click()
            await page.wait_for_timeout(4000)
        elif a.set or a.opacity is not None:
            await page.wait_for_timeout(1500)
        a.out.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(a.out))
        await browser.close()
    print(f"-> {a.out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--url", default="http://localhost:8765/")
    ap.add_argument("--slug")
    ap.add_argument("--floor")
    ap.add_argument("--center", help="lng,lat")
    ap.add_argument("--zoom", type=float)
    ap.add_argument("--pitch", type=float)
    ap.add_argument("--bearing", type=float)
    ap.add_argument("--set", action="append", default=[], metavar="CHECKBOX=on|off")
    ap.add_argument("--opacity", type=int)
    ap.add_argument("--size", default="1400x900")
    ap.add_argument("--wait", type=int, default=7)
    ap.add_argument("--channel", default="chrome", help="chrome (installed Google Chrome) or chromium (playwright's)")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
