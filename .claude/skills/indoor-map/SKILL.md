---
name: indoor-map
description: Create or edit a Mapbox indoor map (Mapbox indoor data model GeoJSON) of a real building from a natural-language request. Use when the user names a facility and wants an indoor/floor map ("丸ビルの屋内マップを作って", "make an indoor map of KITTE"), or asks to change an existing one ("2階の北側を会議室に", "この部屋を広く", "元に戻して"). Researches the building on the web, writes configs/<slug>.json, builds, and keeps a live GL JS preview in sync. Floor-map images, new site importers, sourcing quality and publishing are separate skills (indoor-floor-image, indoor-importer, indoor-quality, indoor-publish).
---

# Indoor map: generate and refine

The config `configs/<slug>.json` is the single source of truth. Never edit
`output/**/indoor.geojson` by hand — edit the config and rebuild.
Schema: `indoor_gen/schema.json`. Writing guide and edit recipes: [config_guide.md](config_guide.md).
Python: always `.venv/bin/python` (create with `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` if missing).

Decide the mode first: if `configs/<slug>.json` for the building already exists and the user is asking for changes → **B. Edit loop**. Otherwise → **A. First build**.

## A. First build

1. **Parse the request**: building name/address, which floors matter, any layout hints ("1階は店舗中心"). If the building is ambiguous (several candidates, e.g. a station complex), ask with AskUserQuestion.
2. **Identify the footprint**: `.venv/bin/python main.py locate "<name>"` (or `--lat/--lon`). Prefer a candidate with `matches_search: true` / `contains_point: true` and a plausible area. Put `"source": {"type": "osm_id", ...}` in the config so rebuilds are stable. If the polygon looks wrong (tiny area, a roof, a whole block), say so and try `--radius`, another candidate, or ask the user.
   **Never take the footprint (or any other shape) from Mapbox's own map tiles** (Mapbox Streets, "the Mapbox Building"): the Mapbox Product Terms forbid tracing, deriving or extracting content from the service (1.6), caching or storing it (1.9) and redistributing it (1.10), and the indoor data ends up in files that are shared and published. There is no command for it any more (`mapbox-footprint` was removed). Keep the OSM outline as the footprint when it is plausible; when it is not, draw one as described next. The preview is drawn over the Mapbox basemap, and a floor map is placed on it by eye or by `basemap` (that is using the service, not copying from it).
   **OSM can hold the old building** (a new or rebuilt building whose way is still the pre-construction shape: area far from the gross floor area ÷ floors, a plain rectangle where the plan is chamfered). Then draw the footprint from the building's own floor plan instead: trace its outer wall at real scale (use the plan's printed dimensions; see `indoor-floor-image`), write it as `configs/plans/<slug>/footprint.geojson` and set `"source": {"type": "geojson", "path": "plans/<slug>/footprint.geojson"}`. Keep the OSM way noted in `provenance.sources`. **Place the plan itself with `basemap --true-width-m <the plan's printed width>`** (`indoor-floor-image`), not by the default fit: that fit stretches the drawing to the old, larger footprint (1.4x too big for PMO新橋II). For a shopping mall whose OSM outline leaves out an island or a wing, the footprint is the union of the shops traced from the floor maps (courtyards filled in), saved the same way. Check that traced rooms of independently placed floors fall (almost) 100% inside it. Changing the footprint changes the local frame, so ratio rooms shift a little and re-traced plan files differ by float noise (`git checkout` them if only noise).
   **Default policy: the footprint is the OSM outline (or, in the cases above, one drawn from the building's own plan or traced shops), and traced rooms may stick out of it.** Do not redraw the footprint from a schematic that is not to scale (Haneda T2's maps are 1.3-1.5x the real area; a footprint drawn from them was tried and reverted; a plan with printed dimensions is fine), and do not drop or clip a traced room because it falls outside the footprint: plan rooms are not cut to it, the floor slab / wall follow them. Report the share that lies inside (the `trace` output) so the user can see how far a map and the footprint disagree; fixing that is for `nudge-map` or hand edits, not for deleting rooms. The only exception is the old-building case above (draw it from the plan). A floor `outline` taken from a floor map is optional; give such a file `properties.follows_map: true` so `nudge-map` moves it with the maps.
   For towers and stepped buildings run `.venv/bin/python main.py parts configs/<slug>.json` (after writing a minimal config with the source): it saves OSM `building:part` shapes inside the footprint to `configs/plans/<slug>/parts/`. Use a part as `outline` for the floors it covers (judge by its `height` / `building:levels`) instead of guessing the tower's size and position.
3. **Research** (WebSearch / WebFetch; run independent searches in parallel):
   - official floor guide / フロアガイド / 館内マップ: use per floor, tenants, where they are (north side, station side…)
   - floors above/below ground, building height, 基準階面積 or 延床面積
   - OSM tags from `locate` (`levels`, `underground_levels`, `height`) as a cross-check
   Record every source you rely on in `provenance.sources` (id, url, note). Mark each room `confidence: "sourced"` (seen in a source) or `"estimated"` (your inference). Do not invent tenant names; use generic names (ショップ, オフィス) when unknown.
   **Prefer real shapes over ratios.** If the official floor guide has machine-readable floor maps (SVG maps, tenant outlines in the page's data, e.g. a Nuxt `_payload.json`), import them as plan GeoJSON instead of approximating with ratio strips:
   - marunouchi.com buildings (丸ビル, 新丸ビル, …; only where the internal-only `import-floorguide` exists): `.venv/bin/python main.py import-floorguide configs/<slug>.json --building <url-key> --map-up <direction> --source-id s1 [--floors B1F,1F] [--exclude B1F:33,34]`. Read `--map-up` from the map's edge labels (丸ビル: 丸の内仲通り・皇居側 at the top = `west`). Exclude tenants drawn inside a magnified inset. Check each floor's fit numbers (`fit_*` ≥ 0.85, `outside_footprint_pct` small) and look at `output/<slug>/plan_debug/<floor>.svg` (north up) against the official map.
   - Maps that show only a tower (upper floors drawn at a larger scale): add `--fit-to plans/<slug>/parts/<part>.geojson` so they are fitted to the tower outline, and give those floors the same `outline`.
   - Floor pages drawn as an HTML image map (`<img usemap>` + `<area>` hotspots per tenant, common on shopping-centre sites): `.venv/bin/python main.py import-imagemap configs/<slug>.json --map-up <direction> --source-id s1 --page 1F=<url> --page 2F=<url> [--exclude 3F:4]`. The building outline is taken from the image's opaque area. Then set each plan feature's `category` (from the site's category lists) — the importer leaves it unset. Hotspot links can be wrong (dead shop pages); check names against the map image.
   - Other sites with machine-readable maps (SVG / JSON outlines): use the `indoor-importer` skill to write a similar importer.
   - **Maps that are only images** (PNG/JPG/PDF, e.g. a station yard map drawn as a CSS background, no shapes or hotspots): these are not a reason to fall back to ratio strips. Note each floor's map URL now; after the config is written and built (steps 4–5), run the `indoor-floor-image` skill (section C) for every such floor without being asked — register the image, align it automatically and trace its rooms into a plan — then rebuild. Ratio rooms remain only for floors with no map at all.
   The importer also records where each map image lies (`metadata.basemap`), so the preview can show the official map under the rooms; tell the user about the "Display (compare with the source data)" controls (checkboxes for the source floor map and the OSM outline, a slider for room opacity) for checking the fit. While a map is shown, its tenant areas are tinted yellow (importers record their fill colours as `basemap.tenant_fills`), our grey slab / common area are hidden and room outlines are drawn instead.
   Plan floors still accept ratio `rooms`, which divide the space between the plan rooms (lobbies, atria as `void`).
4. **Write `configs/<slug>.json`** following config_guide.md. Model the floors the user cares about; for towers, detail the podium and group typical office floors with `levels`. Give every room an explicit `id`. Put the user's original request in `provenance.request`.
5. **Validate and build**:
   `.venv/bin/python main.py validate configs/<slug>.json` → fix every error → `.venv/bin/python main.py build configs/<slug>.json`
6. **Start the preview** (once per building): `curl -s localhost:8765/build_info.json` shows which slug is being served. If nothing is serving this slug, start `.venv/bin/python main.py serve <slug>` with `run_in_background: true` (use `--port 8766` etc. if 8765 is taken by another slug). Give the user the URL. If `MAPBOX_ACCESS_TOKEN` is unset the page asks for a token itself.
7. **Write `output/<slug>/research.md`**: sources used, what is sourced vs estimated, open questions.
8. **Report** briefly: footprint used (OSM id, area), floors and main rooms, what was estimated, the preview URL, the build output's `quality_area_pct` (area share traced / listed / estimated / derived; see the `indoor-quality` skill for what they mean), the build output's `estimated_cost_usd` (API list-price estimate of the Claude usage spent on this building so far, shown in the preview's "Cost to build (estimated)"; turns that change the tool's own code count as development, not the building), and how to refine (e.g. 「3階の東側をカフェに」, or click a room in the browser and say 「この部屋を〜」).

## B. Edit loop

Repeat for each user instruction:

1. **Resolve the target**:
   - "この部屋 / this room / 選択した部屋" → read `output/<slug>/selection.json` (`id` = the config's room id, `name`, `level`). If it is missing, ask the user to click the room in the preview.
   - by name / floor / side → find it in the config (the `build` output lists every config room id per floor; ids in `changes` are the feature ids `room-<slug>-<config id>`).
   - Ids on floors expanded from `levels` / `copy_from` (e.g. `L8-office`) come from the defining entry (`L7-office` in `"levels": [7,8,9]`); editing it changes all those floors. If the user means only one floor, split it out into its own entry first.
2. **Translate the instruction into a config edit** with the recipes in config_guide.md. If it needs new facts ("実際のテナントに合わせて"), research first.
3. **Validate → build** (each build snapshots history). The preview updates by itself, keeping the camera and floor, and flashes the changed rooms.
4. **Report the diff** from the build output's `changes` in one or two lines (e.g. "5F: レストラン 60%→45%、会議室(15%)を南側に追加"). If the build output's `quality_change` is not null, add how the sourcing moved (e.g. "推定 40%→25%"); it lists only the grades that changed, as `[before, after]` area shares.
5. "いくらかかった？ / cost" → `.venv/bin/python main.py cost <slug>` (`main.py cost` alone lists every building, `_development` and `_other`).
6. "元に戻して / undo" → `.venv/bin/python main.py undo configs/<slug>.json` (each call steps back one built version).

Re-read the config before every edit so earlier instructions stay intact.

## C. Floor-map images

For a floor map that no importer can read (the user's image / URL, or an image-only official map found in A.3): use the
`indoor-floor-image` skill. Run its whole flow without asking the user to click anything: load → align automatically →
trace the rooms → build → screenshot → report. Do it for every image-only floor during a first build, without being asked.

## E. Sourcing quality

Grades (`traced` / `listed` / `estimated` / `derived`), `main.py report`, researching weak rooms, `compare` and `cost`:
use the `indoor-quality` skill. After every build still report the build output's `quality_area_pct` and `quality_change`.

## D. Publish to a Mapbox tileset

Only when the user asks (「アップロードして」「タイルセットに公開」): use the `indoor-publish` skill. Never upload on your own.

Preview: floors are switched with Mapbox's `IndoorControl` (bottom right, zoom ≥ 16); the chosen floor is shown flat at street level. Tell the user this when they ask how to change floors.
