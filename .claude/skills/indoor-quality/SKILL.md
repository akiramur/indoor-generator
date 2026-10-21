---
name: indoor-quality
description: Audit and improve how well each room of an indoor map is sourced (traced / listed / estimated / derived), compare builds, and check Claude usage cost. Use for "出典の弱いところを調べて", "推定の部屋だけ出典を探して", "どれくらい根拠がある？", "モデル別に比較して", "いくらかかった？", or before publishing when many rooms are estimated.
---

# Indoor map: sourcing quality, comparison and cost

Part of the `indoor-map` workflow: `configs/<slug>.json` is the source of truth (see the `indoor-map` skill and its
`config_guide.md`); never edit `output/**/indoor.geojson`. Python: always `.venv/bin/python`.
Section references like "A.3" and "C" point to the `indoor-map` and `indoor-floor-image` skills.

Every room carries two claims: its **name** (`confidence`: `sourced` = seen in a source, `estimated` = your inference)
and its **shape** (`geometry_source`: `vector` / `imagemap` / `traced` / `drawn` come from a source or a drawn map;
`ratio` is a share of the floor; `remainder` is what is left between rooms). Grades (`indoor_gen/quality.py`, same in the
preview's "Source confidence" panel and its colour toggle):
- `traced` — named in a source and the shape comes from one; `listed` — named in a source, shape guessed (ratio);
- `estimated` — no source; `derived` — common areas left over (not a problem).

`.venv/bin/python main.py report <slug>` prints shares per floor and the weakest rooms (`estimated`, then `listed`,
largest first); `--json` for everything, also saved as `output/<slug>/quality.json`. `publish` warns (never blocks) above 50% weak.

「推定の部屋だけ出典を探して」 / 「出典の弱いところを調べて」:
1. `report <slug> --top 20` and take the `estimated` rooms (then `listed`) in that order. Say how many you will look at.
2. Research each (floor guide, official site, OSM tags; parallel searches). Never invent facts to raise the score:
   - found: set the room's name / category from the source, add the source to `provenance.sources`, set `source` and
     `confidence: "sourced"`. That moves it to `listed` (shape still a ratio), not to `traced`.
   - not found: leave it `estimated` and say so; for spaces that never appear in public sources (機械室, バックヤード) that is the honest answer.
3. To move a `listed` room to `traced` its shape needs a source: import the floor guide's map (A.3) or align and trace a floor-map image (C).
4. Validate → build, then report the build output's `quality_change` and what stayed estimated and why.

## Compare and cost

- `.venv/bin/python main.py compare <a> <b>` compares two builds of one building (e.g. per model): cost, sourced share,
  alignment, room / category agreement. `a` is the reference, `b` the candidate; each is a slug, config path or output
  directory (`b` may be in another worktree).
- `.venv/bin/python main.py cost <slug>` is the estimated Claude usage cost (`main.py cost` alone lists every building,
  `_development` and `_other`). Turns that change the tool's own code count as development, not the building.
