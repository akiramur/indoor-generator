---
name: indoor-floor-image
description: Overlay a floor-map image (PNG/JPG/PDF/URL, e.g. an official floor guide or station map) on a floor of an existing indoor map, align it to the building footprint automatically using OSM landmarks, and trace its rooms into a plan. Use for "この画像を3階に重ねて", "公式のフロアマップを合わせて", "位置合わせをやり直して", "自動配置に戻して", or when the indoor-map skill finds a floor whose only source is an image.
---

# Indoor map: floor-map images aligned by point pairs

Part of the `indoor-map` workflow: `configs/<slug>.json` is the source of truth (see the `indoor-map` skill and its
`config_guide.md`); never edit `output/**/indoor.geojson`. Python: always `.venv/bin/python`.
The config must already exist (first build done); this skill adds or fixes the image on one floor.
Section references like "A.3" point to the `indoor-map` skill.

For a floor map that no importer can read: one the user supplies (file, URL, "the official 3F map"), or an
image-only official map found during a first build (A.3). **Run it as one automatic flow, without asking the user
to click anything: load (1) → align automatically (1) → trace the rooms (3) → build → screenshot → report.**
Trace whenever the floor's rooms are ratio estimates (no `plan`); on a floor that already has a plan from an
importer, the image is only a reference — align it, don't trace. It takes a few minutes per floor; say so up
front. A result the user is not happy with is fixed by hand in the preview (edit mode "Edit floor" → "Floor map" mode: drag / scale the floor map, then "Save this floor only"; see 2).

1. **Load** — 「この画像を3階に重ねて」:
   - Get the image as png / jpg / webp / svg. Convert a PDF page first (`sips -s format png in.pdf --out /tmp/x.png`
     for page 1, or `pdftoppm -png -f N -l N`), and crop wide margins or legends if they dwarf the plan.
   - `.venv/bin/python main.py basemap configs/<slug>.json --level 3 --image <path-or-url> [--map-up west | --north-deg 249] [--fit-to <outline>] [--source-id s3]`
     places the drawing automatically: it is only scaled, turned and moved (never bent) so its outline covers the
     footprint best, and the output reports the overlap (`fit.iou`) and the candidates it found. **Read the top of the image
     (compass, street names) and pass `--map-up`; if the drawing has a north arrow, read its angle instead and pass
     `--north-deg` (degrees clockwise from the image's top to north: 0 = up, 90 = right, 180 = down, 249 = lower left): it settles the
     upside-down ambiguity of a long thin building and starts the fit within a few degrees of the true turn; for a second floor of the same building pass `--like-level <placed floor>`.**
     For a map-app screenshot (terrain / runways all round the building, no blank page) add `--tile-map`: the
     building is read from its floor colours. Screenshots of one floor taken at different zooms can be joined first
     (SIFT + similarity, see the Haneda T2 case); never commit them. A long, thin building looks the same upside down: then the output warns that the direction is ambiguous — don't
     accept that pick without checking a landmark (`landmarks`) or another floor. **Give third-party maps (official sites etc.) as their URL**: the config then keeps
     the URL and the image is only cached under `output/` — never commit such images to the repository. A local file
     (the user's own drawing, a converted PDF page) is copied to `configs/plans/<slug>/maps/<floor>.<ext>`; for a page
     converted from a third-party PDF, ask before committing it. Read `--map-up` from the image (compass, street
     names) if you can; it only affects the starting position.
   - Put the printed `basemap` object on that floor's entry in the config (add the image's source to `provenance.sources`),
     validate, build. A floor in a `levels` group shares one basemap; split it out if only one floor should have it.
   - **Align it yourself first (自動位置合わせ) — don't hand point-picking to the user:**
     1. `.venv/bin/python main.py landmarks configs/<slug>.json` lists named OSM entrances / ticket gates around the
        building with their positions.
     2. Find the same names on the image. Locate the gate / door *symbol* (not the text label) at full resolution:
        crop regions with a pixel grid (PIL; e.g. 520 px squares scaled to 900) and read the pixel coordinates.
        Prefer landmarks on both sides of the building and at its ends; match like with like (conventional-line
        gates vs Shinkansen gates, a dome's gate line vs its outer door).
     3. Check consistency before applying: fit `indoor_gen.align.fit_pairs` (similarity and affine) and drop a pair
        whose residual is far above the rest (it is a wrong match or a wrong OSM node). Typical: a few metres.
     4. Write `output/<slug>/alignment.json` ({level, floor_name, image (URL or config-relative path), image_size,
        mode: "similarity", pairs: [{px, lnglat, label: "<name> (osm node <id>)"}]}) (2+ well-spread pairs), then `.venv/bin/python main.py align --apply configs/<slug>.json`.
     5. Screenshot the floor in the preview to confirm, and report which landmarks were used / dropped.
     6. **Check image and rooms still agree** (do this after every alignment, also a re-alignment, the preview's
        "Save this floor only" / "Apply to all floors", or `nudge-map`): moving the image never moves a plan's rooms. If the floor has
        `plans/<slug>/<floor>-trace.json`, rerun `main.py trace` (step 3) so the rooms follow the new placement. If the
        plan comes from an importer, or was traced by hand, screenshot with and without the map (`show-map=on|off`) and
        compare tenants against the image; if they are off, say so in the report and offer a re-trace. A basemap with
        only `corners` and no `alignment` (never fitted to landmarks) counts as unaligned: align it now, otherwise it
        is only as good as the first rough placement (a whole-station map fitted to one building's outline looks
        "wrong scale").
     If too few named landmarks exist (small buildings), fall back to the building outline's corners, or ask the
     user to fix it in the preview. **If another floor's image is already placed** (its `basemap.corners`), use it as
     the reference instead: pick walls / corners that are the same on both floors (outer wall corners, the building's
     south / west wall; not the lift or stair cores, whose layout often differs by floor), read their pixels on both
     images, convert the placed floor's pixels to lng/lat by bilinear interpolation of its corners, and use those as the
     pairs (label them "= <floor> image's same corner"). Use 3+ pairs so the residuals mean something (a few tenths of a metre
     is good), and note in the report that the placement is inferred through that floor. Site plans that include
     parking / green space: trace only the rooms inside the building, and leave unlabelled spaces out (they stay 共用部).
     **Printed dimensions give the true scale.** A plan with a dimension line (e.g. 23,600 mm) or a stated scale
     fixes px/m; compute it and check the floors already placed against it (the same lift hall / stair should have the
     same area on every floor, and a leasable-area figure such as 283.38 m² should come out within ~10% of the traced
     rooms). Outline-fitted images (importers, or a floor fitted to a stale OSM outline) can be 10% too large: rescale
     them about their centre, re-align the floors that were placed through them, and re-run `trace`. Then, if OSM's
     footprint is the old building, replace it with an outline drawn from this plan (`indoor-map` A.2).
   - Then go straight on to step 3 (trace) — don't stop after the alignment.
   - Manual fix (only when needed): the preview has no point-pair picking any more. Pick the floor → "Edit floor" → the "Floor map" mode (image icon): drag the map to move it, the corner / edge handles to scale it, the slider to rotate → "Save this floor only" (`POST /nudge` with the floor's `level`: moves that floor's map, plan and map-drawn outline and builds) or "Apply to all floors". Then rerun `trace` for that floor (step 1.6).
2. **After a save** — if the user asks about the result, or 「元に戻して」: `output/<slug>/alignment.json` holds the
   pairs and the config's `basemap.alignment` the fit (`rms_m`, pairs). `main.py undo` restores the config's previous corners (plan files are not restored).
   `main.py align --reset <level> configs/<slug>.json` goes back to the
   placement from before the first alignment: `basemap.auto_corners` (recorded on the first apply), or for importer
   floors the plan's own basemap.
   `main.py align configs/<slug>.json` (without `--apply`) re-reports `residuals_m` / `warnings` without changing
   anything. If a pair is several metres off or only the minimum number of pairs was used, suggest adding or
   re-picking points. 
   **Floor maps are never warped.** The image is only scaled, turned and moved (`similarity`); there is no
   `affine` / `piecewise` mode any more. A schematic map that is not to one scale will not line up everywhere: fit it
   to the part the rooms are traced from, and trace by what the drawing shows. Point pairs: 2+, spread apart.
   Re-read the config before your next edit: the save rewrote it.
3. **Trace rooms from the aligned image** (part of the flow, not optional; raster maps have no shapes to import): crop the building's part of the
   image at full resolution with a pixel grid, read each space's outline (walls, colour fills, icons tell what it
   is), and write them as `configs/plans/<slug>/<floor>-trace.json` ([{id, name, category, source, flat?, px:
   [[x, y], ...]}], keeping the old room ids where the space is the same). `.venv/bin/python main.py trace
   configs/<slug>.json --level N --rooms <that file>` maps them through the floor's placement (the image corners) into `plans/<slug>/<floor>.geojson` and reports how much of each lies inside the footprint; set the
   floor's `plan` to it, drop the ratio rooms it replaces, build, and screenshot with and without the map (`tools/screenshot_preview.py --slug <slug> --floor <n> --set show-map=off|on --out x.png`, needs the preview running and `pip install playwright`). Rerun
   `trace` after re-aligning: the pixel outlines stay valid.
   A **vector** chart (an official seating chart PDF / SVG with one fill colour per zone) needs no hand tracing: `tools/svg_zones.py chart.svg 1F-trace.json --image chart.png --legend legend.json` writes the zones (`--list` shows the fills; the legend maps each hex to a name), and `main.py basemap ... --outline-trace 1F-trace.json` places the image by the zones' hull instead of the page's blank area. Then `trace` as below. A chart that is a plan view of a bowl looks the same turned by 180°: settle the direction before accepting the fit: the venue's own interactive map (e.g. a platinumaps.jp embed: `/map/maps/<id>/spots` lists every seating area with lat/lng) gives positions to fit a similarity to the zones' centroids (read the image's up bearing from it), then pass it as `basemap --north-deg` (it fits the hull within a few degrees of it). 東京ドーム: the chart's top is bearing ~323° (outfield north-west, home plate south-east).
   Facility pictograms (toilet / elevator / escalator / stairs / ATM) go in as `{"id": "L1-pict-01", "name": "エレベーター",
   "icon": "elevator", "source": "s1", "point": [x, y]}` entries of the same file (a point POI, no room; see config_guide.md).
   A map that draws them as circles with a legend can be scanned with `tools/detect_pictograms.py` (candidates only: check
   its contact sheets; `--squares R,G,B` for solid-colour square icons). Read coordinates with `tools/crop_grid.py`, write them with `tools/add_points.py` (from a list, or `--from-detect --accept 0:elevator,...`), check the
   result with `tools/overlay_trace.py <image> <trace.json> --out x.png`. **Pictograms** (point POIs) outside the footprint are dropped at build, but **traced rooms stay even where they stick out of the footprint** (the OSM outline, or one drawn from the plan; see the footprint policy in `indoor-map` A.2): report the share inside, never clip or drop them for it. A schematic map (not to scale) can disagree with the footprint by tens of metres; a map-app screenshot (terrain around the building) is placed with `basemap --tile-map`. Rooms that are only named by a label on the map, with a guessed boundary, take `"geometry_source": "ratio"` in the trace file (graded `listed`, not `traced`);
   skip decorative ladders between icons.
   Report: the landmarks used / dropped for the alignment and its RMS, the traced rooms with areas, how much of
   each was clipped by the footprint, what was renamed or merged, and the share left as 共用部・通路.

On a plan floor the image is only a reference: aligning it does not move the plan rooms. Check them against the image after aligning (step 1.6).
