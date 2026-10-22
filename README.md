# indoor-generator

English | [日本語](README-ja.md)

> An independent, personal project. It is not an official Mapbox product and is not supported or endorsed by Mapbox. Mapbox is a trademark of Mapbox, Inc.

A tool that generates indoor maps of real buildings (GeoJSON in the
[Mapbox indoor data model](https://docs.mapbox.com/help/dive-deeper/indoor-mapping/):
`facility` > `structure` > `floor` > `room`) from a natural-language request, and refines them through conversation.

```
"Make an indoor map of Marunouchi Building"
  → footprint (OSM outline, or drawn from the floor plan) + web research (official floor guides, etc.)
  → configs/<slug>.json
  → Python pipeline
  → output/<slug>/indoor.geojson + live preview
  → refine with "make the east side of 3F a café", etc.
```

Claude Code (the skills in `.claude/skills/`) does the research, writes the config and talks to you; a deterministic
Python pipeline generates the output from the config. For the design, see
[docs/architecture-ja.md](docs/architecture-ja.md) (in Japanese).

## Quick start

You use this tool by talking to [Claude Code](https://code.claude.com/docs/en/overview) **from inside a clone of this repository**.
The skills that know how to research a building and drive the pipeline live in this repository (`.claude/skills/`, plus `CLAUDE.md`), and Claude Code
loads them only for the folder it was started in. Started anywhere else, it knows nothing about this tool.

1. Install Claude Code (the terminal CLI, the desktop app and the VS Code extension all work; see its [documentation](https://code.claude.com/docs/en/overview)) and sign in. You also need Python 3.10 or newer (the version it was developed with; older ones are untested) and git. The commands below assume macOS or Linux (on Windows use `.venv\Scripts\...`).
2. Clone this repository and set it up (details in [Setup](#setup)):

   ```sh
   git clone https://github.com/akiramur/indoor-generator.git
   cd indoor-generator
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   cp .env.example .env   # put your Mapbox public token (pk.*) in MAPBOX_ACCESS_TOKEN
   ```

3. Start Claude Code **in that folder** (in the desktop app or VS Code, open the cloned folder as the project instead):

   ```sh
   claude
   ```

4. Type a request into the Claude Code prompt (this is a chat, not a shell command), for example:

   ```
   Make an indoor map of Marunouchi Building
   ```

   Claude Code researches the building, writes `configs/<slug>.json`, builds `output/<slug>/indoor.geojson` and starts the live preview
   at <http://127.0.0.1:8765>. Keep asking for changes in the same session ("Make the north side of 2F a meeting room").
   Claude Code may ask your permission before it runs commands or fetches web pages; read the prompt and approve what you accept.

Using Claude Code needs a Claude subscription or API account and consumes your usage: researching and building one building takes a few minutes of web
searches and fetches, and `main.py cost` prints an estimate at API list prices.

To try the pipeline without Claude Code, see [Run the sample](#run-the-sample).

## Setup

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in the tokens
```

| Variable | Used for |
|---|---|
| `MAPBOX_ACCESS_TOKEN` | Public `pk.*` token, embedded into the preview's `index.html` (if unset, the page asks for one) |
| `MAPBOX_UPLOAD_TOKEN` | **Secret** `sk.*` token (tilesets:read / write / list). Used only by `publish --upload`, never embedded anywhere |

`.env` is git-ignored. Variables already set in the shell take precedence. The `pk.*` token is written into `output/<slug>/index.html`: give it a URL restriction in the Mapbox console if you share that file.

### Mapbox account and tokens

1. Sign up for free at [mapbox.com/signup](https://account.mapbox.com/auth/signup/) and sign in. The username you choose there is your Mapbox username.
2. Open [Access tokens](https://console.mapbox.com/account/access-tokens/). The **default public token** (`pk.*`) is enough for the preview: put it in `MAPBOX_ACCESS_TOKEN`.
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

In a Claude Code session started inside this repository (see [Quick start](#quick-start)), name a building. The `indoor-map` skill handles research, writing the config,
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
| `import-imagemap` | Import real tenant shapes from floor-guide pages drawn as HTML image maps (more site-specific importers can be added as `indoor_gen/importers/*_cli.py`, see the `indoor-importer` skill) |
| `basemap` / `landmarks` / `align` / `trace` | Place, align and trace floor-map images |
| `edit-rooms` | Apply hand edits of room shapes made in the preview (move / add / delete vertices, delete / add rooms) and build |
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
The tool does not copy shapes out of Mapbox's map tiles: the Mapbox Product Terms do not allow tracing, extracting, storing or redistributing that content. Footprints come from OpenStreetMap or are drawn from the floor plan.

Maps built from a third party's floor guide or floor-map image (imported shapes, traces, a published tileset) may be derivative works. Get the
rights holder's permission before you share or publish them.

## Tests

```sh
.venv/bin/python -m pytest -q
```

## Notes

- **Accuracy.** The maps are built from web research and estimates (rooms graded `estimated` / `listed` have no sourced shape) and are approximations. Do not use them for navigation, evacuation or any safety-critical purpose. Provided "as is", without warranty (see `LICENSE`).
- `publish --upload` replaces one tileset shared by all buildings. If the local `output/` is incomplete, buildings disappear from it, so check the dry-run list first. Production needs `--target prod`.
- Third-party material such as official floor-map images is never committed (the config keeps the URL; images are cached under `output/`).
- Map data: footprints and coordinates come from © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors (ODbL), or are drawn from the floor plans you provide. Keep the attribution when you publish maps built with this tool, and check the rights to the floor plans you use.
- The preview server writes configs and plans on your machine, so it accepts only requests addressed to localhost and refuses cross-site POSTs. `serve --host 0.0.0.0` opens it to your network (no sign-in): use it only on a network you trust.
- OSM APIs are used within Nominatim's 1 req/s limit. Footprints are cached in `output/<slug>/footprint_cache.geojson`.
