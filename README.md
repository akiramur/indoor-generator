# indoor-generator

English | [日本語](README-ja.md)

> An independent, personal project. It is not an official Mapbox product and is not supported or endorsed by Mapbox. Mapbox is a trademark of Mapbox, Inc.

A tool that generates indoor maps of real buildings (GeoJSON in the
[Mapbox indoor data model](https://docs.mapbox.com/help/dive-deeper/indoor-mapping/):
`facility` > `structure` > `floor` > `room`) from a natural-language request, and refines them through conversation.

```
"Make an indoor map of Marunouchi Building"
  → footprint (OSM to locate, Mapbox Building shape) + web research (official floor guides, etc.)
  → configs/<slug>.json
  → Python pipeline
  → output/<slug>/indoor.geojson + live preview
  → refine with "make the east side of 3F a café", etc.
```

Claude Code (the skills in `.claude/skills/`) does the research, writes the config and talks to you; a deterministic
Python pipeline generates the output from the config. For the design, see
[docs/architecture-ja.md](docs/architecture-ja.md) (in Japanese).

## Setup

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in the tokens
```

| Variable | Used for |
|---|---|
| `MAPBOX_ACCESS_TOKEN` | Public `pk.*` token, embedded into the preview's `index.html` (if unset, the page asks for one) |
| `MAPBOX_UPLOAD_TOKEN` | **Secret** `sk.*` token (tilesets:read / write / list). Used only by `publish --upload`, never embedded anywhere |

`.env` is git-ignored. Variables already set in the shell take precedence.

### Mapbox account and tokens

1. Sign up for free at [mapbox.com/signup](https://account.mapbox.com/auth/signup/) and sign in. The username you choose there is your Mapbox username.
2. Open [Access tokens](https://account.mapbox.com/access-tokens/). The **default public token** (`pk.*`) is enough for the preview: put it in `MAPBOX_ACCESS_TOKEN`.
3. Only if you want to upload a tileset: click **Create a token**, tick `tilesets:read`, `tilesets:write` and `tilesets:list`, and put the resulting **secret** token (`sk.*`, shown once) in `MAPBOX_UPLOAD_TOKEN`. Never commit it or put it in a web page.

### `publish.json` (only for `publish` / `viewer`)

`main.py publish` uploads all built buildings as one tileset, whose ID is `<your Mapbox username>.<tileset name>`. The username is not
secret, but it belongs to you, so it is not hard-coded in the repository: copy the example and edit it. The file is git-ignored.
Without it, `publish` has no username (the preview and `build` do not need the file).

```sh
cp publish.example.json publish.json
```

```json
{
  "username": "your-mapbox-username",
  "default_target": "dev",
  "targets": { "dev": "indoor-dev", "prod": "indoor" },
  "minzoom": 14,
  "maxzoom": 16
}
```

| Key | Meaning |
|---|---|
| `username` | Your Mapbox account username (the owner of the tileset) |
| `targets` | Target name → tileset name. `dev` is published as `<username>.indoor-dev`, `prod` as `<username>.indoor` |
| `default_target` | Used when `--target` is not given. Keep it `dev`, so production is only reached by asking for it (`--target prod`) |
| `minzoom` / `maxzoom` | Zoom range of the generated tiles |

`publish` is a dry run unless you pass `--upload`.

## Usage

### From Claude Code (the normal way)

Open Claude Code in this repository and name a building. The `indoor-map` skill handles research, writing the config,
building and starting the preview.

- Create: "Make an indoor map of Marunouchi Building"
- Edit: "Make the north side of 2F a meeting room"; click a room in the preview and say "make this room bigger"; "undo"
- Floor-map images: "Overlay this image on 3F" (`indoor-floor-image`)
- Sourcing: "Which rooms are poorly sourced?", "How much did this cost?" (`indoor-quality`)
- Publish: "Upload it" (`indoor-publish`; runs only when you ask)

### Run the sample

```sh
.venv/bin/python main.py build samples/building_config.json
.venv/bin/python main.py serve marubiru-sample   # http://127.0.0.1:8765
```

Switch floors in the preview with Mapbox's `IndoorControl` (bottom right, zoom 16 or higher).
The preview is available in Japanese and English: English is the default; the button at the top right switches to Japanese (the choice is remembered; `?lang=ja` / `?lang=en` in the URL forces one). Room names and other building data stay as sourced.

### Main commands

Every command prints its result as JSON to stdout, except `report`, which prints a text table by default (`--json` for JSON).

| Command | What it does |
|---|---|
| `locate` / `parts` | Find OSM building candidates; fetch `building:part` shapes |
| `validate` / `build` / `serve` / `undo` | Validate a config, generate, live preview, step back one version |
| `import-imagemap` | Import real tenant shapes from floor-guide pages drawn as HTML image maps (site-specific importers such as `import-floorguide` exist in some checkouts) |
| `basemap` / `landmarks` / `align` / `trace` | Place, align and trace floor-map images |
| `edit-rooms` | Apply hand edits of room shapes made in the preview (move / add / delete vertices, delete / add rooms) and build |
| `mapbox-footprint` | Use the Mapbox Building's shape (Mapbox Streets `building` layer) as the footprint instead of the OSM outline; `--realign` also moves the plans and floor maps to fit it (one similarity transform), `--dry-run` only reports |
| `report` / `compare` / `cost` | Sourcing quality, compare two builds, estimated Claude usage cost |
| `publish` / `viewer` | Publish all buildings as one tileset (dry run and dev by default); check the published result |

Run `main.py <command> --help` for the options.

## Layout

| Path | Contents |
|---|---|
| `configs/<slug>.json` | Building config (source of truth; never edit `output/` by hand). The public repository ships none: create your own with the `indoor-map` skill, or start from `samples/` |
| `configs/plans/<slug>/` | Plan GeoJSON per floor (rooms with real shapes) |
| `publish.json` | Local publish settings (git-ignored; copy `publish.example.json`) |
| `output/<slug>/` | Generated files (git-ignored) |
| `indoor_gen/` | The pipeline |
| `tools/` | Helper scripts for reading floor-map images (pictogram candidates, trace overlay, pixel-grid crops, preview screenshots) |
| `.claude/skills/` | Claude Code skills (`indoor-*`, plus `mapbox-*` copied from the official repository) |
| `samples/` | Sample configs |
| `tests/` | pytest |
| `docs/` | Architecture document, etc. |

## Importers and third-party data

`import-imagemap` (and any site-specific importer) reads the public floor-guide pages of shopping sites. Check each site's terms of use and
robots.txt before using them; they send a plain User-Agent, cache every download under `output/` and are meant for occasional,
personal use. Do not commit third-party images or SVGs (`configs/plans/*/maps/` is git-ignored); the config keeps the URL.
`mapbox-footprint` uses Mapbox Streets data through your own token, under the Mapbox terms of service.

## Tests

```sh
.venv/bin/python -m pytest -q
```

## Notes

- `publish --upload` replaces one tileset shared by all buildings. If the local `output/` is incomplete, buildings disappear from it, so check the dry-run list first. Production needs `--target prod`.
- Third-party material such as official floor-map images is never committed (the config keeps the URL; images are cached under `output/`).
- Map data: footprints and coordinates come from © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors (ODbL) and, with `mapbox-footprint`, from Mapbox Streets (under the Mapbox terms of service). Keep the attribution when you publish maps built with this tool.
- The preview server writes configs and plans on your machine, so it accepts only requests addressed to localhost and refuses cross-site POSTs. `serve --host 0.0.0.0` opens it to your network (no sign-in): use it only on a network you trust.
- OSM APIs are used within Nominatim's 1 req/s limit. Footprints are cached in `output/<slug>/footprint_cache.geojson`.
