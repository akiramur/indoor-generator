---
name: indoor-publish
description: Publish the built indoor maps to a Mapbox tileset (MTS) and check the upload. Use ONLY when the user explicitly asks to upload / publish ("アップロードして", "タイルセットに公開", "本番に反映", "publish the indoor maps", "show me the published tileset"). Never run on your own after a build.
---

# Indoor map: publish to a Mapbox tileset

Never upload on your own. `--upload` and `--target prod` each need an explicit request from the user in this conversation.
Python: always `.venv/bin/python`. Config: `publish.json` (`username`, `targets` dev → `indoor-dev` / prod → `indoor`, `default_target`, zooms).

All built buildings share ONE tileset, and an upload replaces its whole source from the local `output/*/indoor.geojson`
(samples skipped). So an incomplete `output/` means buildings disappear from the tileset.

## Flow

1. **Dry run**: `.venv/bin/python main.py publish` lists the buildings that would go in and writes
   `output/_publish/indoor-<target>.ndjson`. Show the list to the user and check it is complete (build any missing
   building first; `--only` / `--exclude` limit the buildings).
   - Read the warnings: it warns (never blocks) about buildings whose estimated + guessed-shape room area is above
     `--warn-weak-pct` (default 50). Tell the user which ones; offer to research them (`indoor-quality` skill).
2. **Token**: `--upload` needs a **secret** `sk.*` token (tilesets:read/write/list) in `$MAPBOX_UPLOAD_TOKEN`.
   If it is unset, ask the user to put it in `.env` (template: `.env.example`). Never ask them to paste it into the
   chat, and never put it in `index.html` or any file that is committed.
3. **Target**: default is dev (`indoor-dev`). Production (`indoor`) only with `--target prod`, and only when the user
   says so ("本番に"). If they only say "アップロードして", use dev and say which target you used.
4. **Upload**: `.venv/bin/python main.py publish --upload [--target prod]`. It replaces the tileset source, updates
   the recipe, publishes and waits for the job.
5. **Check**: `.venv/bin/python main.py viewer` writes `output/_publish/viewer-<tileset>.html` (the published tileset
   with `IndoorControl` over the official style; `--target dev|prod`, `--serve [--port N]` to serve it over http). Offer it to the user.
6. **Report**: target, buildings uploaded, weak-sourcing warnings, job result, and the printed `style_snippet`.

## Styles

Do not edit the user's style. Report the printed snippet: source `mapbox://<user>.indoor`, source-layer `indoor`,
filter by `building` / `level`. The snippet's `indoor-poi` layer (elevator / stairs / toilet point rooms) needs the
`poi-<icon>` images: `poi_badges` lists them, the viewer draws them on a canvas, and a user's style has to add them with `map.addImage`.

Floors are switched with Mapbox's `IndoorControl` (bottom right, zoom ≥ 16); the chosen floor is shown flat at street level.
