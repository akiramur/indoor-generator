# Config guide

Schema: `indoor_gen/schema.json`. Example: `samples/building_config.json`.

## How a floor is split

`rooms` are cut **in order**. Each room takes `ratio` × (the whole floor area) from the
`side` it names (west / east / north / south) of whatever is still unallocated; the next room
works on the remainder. `side: "rest"` (no `ratio`) takes everything left and must be last.
If ratios sum to < 1 and there is no `rest`, the leftover becomes an `L{n}-unassigned` room.

- Directions are relative to the building's main axis snapped to the nearest compass direction
  (`orientation: "auto"`), so "north" is the side that faces roughly north.
- Order matters: a room cut from `west` after a `south` cut only spans the northern remainder.
  Cut the strips that should run the full length of the building first.
- `subdivide: {"count": N, "axis": "long"|"short"}` splits a room into N equal parts laid out
  along its long (default) or short dimension. Ids become `<id>-1 … <id>-N`.
- Heights: a floor's `height` is floor-to-floor (default `defaults.floor_height`). Base heights
  accumulate upward from 0 for ground-and-above levels; negative levels stack downward.
- `levels: [a, b, …]` + `rooms`: same layout on several floors. `copy_from: n`: reuse level n's
  rooms. Ids are rewritten per floor (`L7-office` → `L8-office`).
- `void: true` reserves area but draws nothing: atria (吹抜) on upper floors, and the part of
  a podium that a tower does not cover. For a centred tower, cut `void` strips from north,
  south, west and east first; the remaining centre is the tower floor. Void rooms still need a `ratio` (or `side: "rest"`).
- `color` overrides the category colour. Categories with default colours: retail, restaurant,
  office, lobby, hall, service, circulation, connector (EV/stairs/escalators; becomes
  `class: connector`), parking, meeting, other.

## Floor outlines (towers, setbacks)

`"outline": "plans/<slug>/parts/way-123.geojson"` limits a floor to that polygon (within the
footprint) — typically an OSM `building:part` saved by `main.py parts`. Ratios on such a floor
are shares of the outline's area, and the slab covers only the outline. On a plan floor, plan
rooms that reach past the outline extend the floor (official maps are usually more detailed
than OSM parts). The same holds for the footprint itself: plan rooms are never cut to it (see the footprint policy in the `indoor-map` skill, A.2). Prefer this over carving a tower out of the podium with `void` strips.

## Plan floors (real room shapes)

`"plan": "plans/<slug>/<floor>.geojson"` (path relative to the config) gives rooms with real
outlines: a WGS84 FeatureCollection whose features have `properties.id` and `properties.name`
(optional `category`, `color`, `source`, `confidence`, `void`, `flat`). Plan rooms are placed
first, trimmed only by earlier rooms (not cut to the footprint, which is coarse: they may stick out of it, and the floor slab / wall follow them); ratio `rooms` on the same floor then divide what is left
(ratios are still shares of the whole floor); anything remaining becomes `L{n}-common`
「共用部・通路」, drawn flat at floor level. `copy_from` copies the plan too.
A plan feature with a **Point** geometry and `properties.icon` (`toilet`, `toilet-male`, `toilet-female`, `elevator`, `escalator`,
`stairs`, `atm`) is a facility pictogram's position: it becomes a point POI of its own (no room, nothing cut from the
floor). `import-floorguide` (internal only) writes them from the marunouchi.com SVG pictograms (`PICTOGRAMS` signature table in
`importers/marunouchi_floorguide.py`; add a signature after looking at `tools/svg_pictogram_sheet.py`);
for raster maps, read the pictogram positions yourself and write the Points. Stairs drawn on those SVGs come in as `階段` rooms.

Every room records where its shape came from, shown in the preview (形状 / 位置) and kept on the
output features as `geometry_source` and `georef` (+ `georef_mode`, `georef_rms_m`):
`vector` (tenant shapes imported from the source), `imagemap` (`<area>` hotspots), `traced`
(Claude traced a raster map), `drawn` (coordinates Claude wrote without a drawing), `ratio`,
`remainder`; placed by `outline_fit`, `point_pairs`, `direct` or `footprint`. Importers and
`trace` write these into the plan's `metadata`. **A plan file you write by hand must say
`"metadata": {"geometry_source": "drawn", "georef": {"method": "direct"}}`**; a feature can
override either in its properties.

Editing plan rooms: change `name` / `category` / `color` in the plan GeoJSON's properties
(keep `id`). To drop a room, delete its feature (its area becomes common space). Re-running
`import-floorguide` overwrites the plan files, so re-apply manual property edits afterwards
or record them in the config as ratio rooms instead.

## Floor-map images (`basemap`)

`"basemap": {"image": "plans/<slug>/maps/3F.png", "corners": [[lng, lat] ×4], "source": "s3"}` on a floor shows
that image under the floor in the preview (corners: top-left, top-right, bottom-right, bottom-left; it overrides a
plan's own basemap). Never write corners by hand — take them from `main.py basemap` (first placement) or
`main.py align` (from the user's point pairs), which also adds an `alignment` record and keeps the earlier
automatic placement as `auto_corners` (restored by `align --reset`).

`flat: true` on any room draws it at floor level instead of full ceiling height.

## Turning research into ratios

| Information found | How to model it |
|---|---|
| Tenant list per floor, no positions | One room per use group sized by tenant count; `subdivide` for rows of shops; `confidence: estimated` |
| Floor map image/PDF with positions | Read the positions; choose `side` from where each block sits; `confidence: sourced` |
| Typical office floor | core (EV/stairs/WC) 10–15 % `circulation` + office `rest` |
| Retail podium | circulation 15–25 % + shops, `subdivide` 3–8 |
| Station-facing / street-facing entrance | lobby or hall on that side, 10–25 % |
| Height and floor count known | floor height ≈ height ÷ floors (podium floors are usually taller: 4.5–6 m) |

## Edit recipes (natural language → config)

| Instruction | Edit |
|---|---|
| 「〜を会議室に変えて」 | change `name` and `category` of that room (keep its `id`) |
| 「もっと広く」「30%に」 | change `ratio`; a `rest` room absorbs the difference, otherwise shrink a neighbour so the sum stays ≤ 1 |
| 「北側に〜を追加」 | insert a room with `side` + `ratio` + new `id` before the `rest` room |
| 「削除して」 | remove the room; its area goes to `rest` / unassigned |
| 「店舗を4つに分けて」 | `subdivide: {"count": 4}` |
| 「2つの部屋をまとめて」 | replace both with one room whose ratio is the sum |
| 「色を青に」 | `color: "#2F6FDE"` |
| 「3〜5階も同じに」 | `{"levels": [3,4,5], "copy_from": 2}` (remove existing entries for those levels) |
| 「8階だけ変えて」(8 is in `levels: [7,8,9]`) | split into `levels: [7,9]` and a separate `level: 8` entry, then edit 8 |
| 「階高を4mに」 | floor `height: 4.0` |
| 「地下1階を追加」 | add `{"level": -1, ...}` |
| 「東西が逆」 | change `side`, or set `building.orientation` to `"north"` or an angle |
| 「別の建物だった」 | `locate` again, change `building.source`, build with `--refresh` |

Keep `id`s stable for rooms that conceptually stay the same — the preview highlight and diffs rely on them.
