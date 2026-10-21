---
name: indoor-importer
description: Write a new importer that reads a floor-guide website's machine-readable floor maps (SVG, image-map hotspots, embedded JSON) into plan GeoJSON for the indoor map generator. Use when the user wants a building whose official floor guide is not covered by import-floorguide (marunouchi.com) or import-imagemap (HTML <img usemap> pages), e.g. "このサイトのフロアガイドから店舗の形を取り込みたい", "importer を追加して", or when indoor-map A.3 finds machine-readable maps no existing importer reads.
---

# Indoor map: add a floor-guide importer

Part of the `indoor-map` workflow (see that skill and its `config_guide.md`). An importer turns a site's tenant shapes
into `configs/plans/<slug>/<floor>.geojson` (geometry_source `vector` / `imagemap`), which is better than ratio strips
and cheaper than tracing an image. Python: always `.venv/bin/python`.

## 1. Is an importer the right tool?

Check in this order and stop at the first that fits:
1. **Existing importers**: `main.py import-imagemap` and, in the internal checkout only, `main.py import-floorguide` (marunouchi.com Nuxt payload + SVG)
   (any page with `<img usemap>` + `<area>` hotspots). Try these first; `import-imagemap` covers many shopping-centre sites.
2. **Floor map is only an image** (no shapes, no hotspots): not an importer job. Use the `indoor-floor-image` skill.
3. **The site has shapes in some machine-readable form**: SVG paths per tenant, a JSON/`_payload` with outlines, canvas
   or Leaflet geometry loaded from an API. → write an importer. Look at the page source and the network calls
   (`curl` the HTML, grep for `.svg`, `.json`, `polygon`, `coords`, `usemap`, `__NUXT__`).
4. **Only one or two floors**: tracing the image by hand (`indoor-floor-image`) is usually faster than an importer.

Before scraping: read the site's terms / robots.txt, send the `USER_AGENT` the existing importers use, cache every
download under `output/<slug>/sources/<importer>/` (never re-fetch on each run, offer `--refresh`), and never commit
third-party images or SVGs to the repository (`configs/plans/*/maps/` is git-ignored; config keeps the URL).

## 2. The contract

A module `indoor_gen/importers/<site>_floorguide.py` with the same shape as `imagemap_floorguide.py` (read it first;
it is the shortest) and, if present, `marunouchi_floorguide.py`:

- `fetch_floor(s)(...)` → per floor: `name` (`"1F"`, `"B1F"`), `page`, the cached image/SVG, its URL, and
  `tenants`: `[{"no": 1, "name": ..., "href": ..., "pin": <shapely geometry in map pixels>}]`. Drop slivers (area < ~4 px).
  Reuse `importers/common.floor_level(name)` for the level number.
- `analyze_floor(floor, footprint_local, map_up, exclude)` → dict with `floor`, `outline` (the building outline drawn on
  the map, in map pixels), `canvas` (width, height), `tenants` (minus the `exclude` set of `no`s), `t_svg_local`
  (transform map px → local metres from `georef.fit(outline, footprint_local, base=georef.FLIP_Y,
  orientations=[(georef.MAP_UP_TURNS[map_up], False)])`), `fit2` (that fit's IoU) and `report`
  (`floor`, `tenants`, `excluded`, `fit_map_to_footprint`, `warnings`).
  Find the outline from the drawing itself (an SVG outline path, or `imagemap`'s opaque-region trick).
- `build_plan(a, footprint_local, to_wgs84, map_up, source_id)` → `(FeatureCollection, report)`. Each feature:
  `properties` `id` (`L<level>-<slug>`, unique, stable), `name`, `category` (see `config_guide.md`; set it from the
  site's categories where it has them), `map_no`, `confidence: "sourced"`, `source`, optional `url`. Collection
  `metadata`: `source`, `floor`, `map_up`, `geometry_source` (`vector` / `imagemap`), `georef: {"method": "outline_fit"}`,
  `basemap` (`url`, `corners` = map canvas corners through `t_svg_local`; optional `tenant_fills` colours so the
  preview can tint tenants). Keep tenants inside the drawn building (`g.intersection(drawn)`); report
  `outside_footprint_pct` and warn above 10%.

If the map also draws **stairs** or **facility pictograms** (toilet / lift / escalator / ATM), have `build_plan` add them too, as
`marunouchi_floorguide` (internal only) does with the same `t_svg_local`: stairs as `階段` rooms (`category: connector`, ids `L<level>-stairs-NN`,
`stair_features`) and pictograms as `Point` features with `properties.icon` (ids `L<level>-pict-NN`, `pictogram_features`; the plan
accepts Points, see `config_guide.md`). Detect them from the drawing's own structure (box + parallel treads; a glyph signature
table filled with `tools/svg_pictogram_sheet.py`); report their counts in the floor's `report`. Ignore what the drawing does not
show; never place a stair or icon from a ratio.

Reuse `indoor_gen/georef.py` (`fit`, `apply`, `compose`) and `importers/common.share_frames` / `common.debug_svg` (called by
`main.py:_write_plans`); do not reimplement fitting.

## 3. Wire it in

1. `main.py`: add a subparser next to `import-imagemap` (same flags: `config`, `--map-up` required, `--exclude FLOOR:N,N`,
   `--source-id`, `--fit-to`, `--refresh`, plus whatever the site needs, e.g. `--building`) and a `cmd_import_<site>`
   modelled on `cmd_import_imagemap`: `_import_setup(args)` → fetch per floor → `analyze_floor` →
   `_write_plans(analyses, importer, ...)`.
2. `tests/test_<site>.py`: offline tests with a small inline page / SVG fixture, like `tests/test_imagemap.py`
   (parser finds the shapes, outline detection, scaling to the display size). No network in tests.
3. Docs: one line in `CLAUDE.md` (Layout → `indoor_gen/importers/`, `main.py` command list) and a bullet in the
   `indoor-map` skill's A.3 list, next to the marunouchi / imagemap entries, saying which kind of site it handles.

## 4. Run and check (per building)

1. `.venv/bin/python main.py import-<site> configs/<slug>.json --map-up <direction> --source-id sN ...`
   (read `--map-up` from the map's edge labels; a minimal config with `building.source` must exist first).
2. Per floor check the fit numbers, but read them correctly: `fit_map_to_footprint` (IoU of the drawn outline and the
   OSM footprint) is high (≥ 0.85) only when the drawing fills the footprint (marunouchi.com); for a mall whose
   drawing differs from the OSM outline it is ~0.6 whatever `--map-up` is (a typical mall page: about 0.6 in all four directions).
   **Choose `--map-up` by `outside_footprint_pct`** (the share of tenant area outside the footprint: the right
   direction gives a few %, wrong ones 10–50%), and the warnings must be empty. If the fit stays mediocre, align the
   floor afterwards with point pairs (`indoor-floor-image`, `align`): such plans can be refined that way
   (similarity). Open `output/<slug>/plan_debug/<floor>.svg` (north up) beside the official map and compare
   tenant names with the map image: hotspot links and alt text are sometimes wrong.
3. Exclude tenants drawn in an inset (`--exclude`), set missing `category` values, add `"plan"` to each floor in the
   config, validate, build; then `indoor-quality` to see the sourcing shares.
4. `.venv/bin/python -m pytest -q` before committing; commit the importer, its tests and docs separately from the
   building's config. Report: the site and data kind found, fit numbers per floor (with the chosen `--map-up` and why), tenants imported / excluded, warnings.
