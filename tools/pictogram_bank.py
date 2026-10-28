"""A bank of facility pictograms that grows with every map you read, so the next map needs fewer clicks.

Every pictogram you accept (elevator, stairs, escalator, toilet, ...) or reject (a ticket machine, a baggage icon) is kept as a
small 24 x 24 black-and-white thumbnail with its label. A new map's candidates are then labelled by the nearest thumbnail,
with an agreement score and a margin over the next-best label; you still look at the contact sheet, but only at the doubtful ones.

The thumbnails are 24 x 24 bits (a glyph, not the map), and the bank lives under `output/_pictograms/` by default, which git
ignores: the maps themselves are third-party and never committed.

    # after reading a map: the candidates you accepted / rejected (indices of the contact sheet from detect_pictograms.py)
    .venv/bin/python tools/pictogram_bank.py add map.png found.json --accept 0:elevator,3:stairs --reject 5,6 --map jr-1f
    # the pictograms already in a floor's trace (point entries), e.g. after tools/add_points.py
    .venv/bin/python tools/pictogram_bank.py harvest map.png configs/plans/<slug>/1F-trace.json --size 34 --map jr-1f
    # a new map: label its candidates (squares of that colour), see how sure each is
    .venv/bin/python tools/pictogram_bank.py classify other.png --squares 36,24,21,25,30,60 --write found.json
    .venv/bin/python tools/pictogram_bank.py loo     # leave-one-out accuracy of what is in the bank
    .venv/bin/python tools/pictogram_bank.py list

`found.json` written by `classify` has the predicted `icon`, `score` (agreement), `margin` and `sure` on each candidate, so
`tools/add_points.py trace <trace.json> --from-detect found.json --image map.png --accept 0:elevator,...` takes it as it is
(`--image` is the path as given to `classify`); `classify` prints the accept list for the candidates it is sure of
(`--sure-agreement` 0.9 and `--sure-margin` 0.08 by default; a sure `other` is never listed). Look at a contact sheet of the
rest, accept or reject them with `add`, and the bank gets better. Tried on JR East's maps: 1F's 34 icons labelled B1's 133
candidates (stairs / escalator / elevator / ticket machine / baggage), 40 of 40 sampled checked by eye right.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None
HERE = Path(__file__).resolve().parent
DEFAULT_BANK = HERE.parent / "output" / "_pictograms" / "bank.json"
N = 24             # thumbnail side, pixels
SHIFT = 2          # a candidate box can be a pixel or two off: the best of the shifts counts
OTHER = "other"    # a pictogram we do not map (ticket machine, baggage, information)


def thumbnail(img: Image.Image, cx: float, cy: float, w: float, h: float) -> np.ndarray:
    """N x N boolean glyph of the box (cx, cy, w, h): grey, resized, split at the middle of its own range."""
    box = (int(round(cx - w / 2)), int(round(cy - h / 2)), int(round(cx + w / 2)), int(round(cy + h / 2)))
    g = np.asarray(img.convert("L").crop(box).resize((N, N), Image.LANCZOS)).astype(float)
    lo, hi = g.min(), g.max()
    return g > (lo + hi) / 2 if hi - lo > 20 else np.zeros((N, N), bool)


def pack(bits: np.ndarray) -> str:
    return np.packbits(bits.reshape(-1)).tobytes().hex()


def unpack(hexs: str) -> np.ndarray:
    return np.unpackbits(np.frombuffer(bytes.fromhex(hexs), np.uint8))[:N * N].reshape(N, N).astype(bool)


def agreement(a: np.ndarray, b: np.ndarray) -> float:
    """Share of equal pixels, best over shifts of up to SHIFT pixels (edges compared where the two overlap)."""
    best = 0.0
    for dy in range(-SHIFT, SHIFT + 1):
        for dx in range(-SHIFT, SHIFT + 1):
            ya, yb = (slice(dy, N), slice(0, N - dy)) if dy >= 0 else (slice(0, N + dy), slice(-dy, N))
            xa, xb = (slice(dx, N), slice(0, N - dx)) if dx >= 0 else (slice(0, N + dx), slice(-dx, N))
            best = max(best, float((a[ya, xa] == b[yb, xb]).mean()))
    return best


def load_bank(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))["entries"]
    except (OSError, ValueError, KeyError):
        return []


def save_bank(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8")


def classify(bits: np.ndarray, bank: list[dict], exclude: int | None = None) -> dict:
    """{"label", "agreement", "margin"}: the label of the nearest thumbnail, and how far ahead of the next label it is."""
    best: dict[str, float] = {}
    for i, e in enumerate(bank):
        if i == exclude:
            continue
        s = agreement(bits, unpack(e["bits"]))
        if s > best.get(e["label"], 0.0):
            best[e["label"]] = s
    if not best:
        return {"label": None, "agreement": 0.0, "margin": 0.0}
    ranked = sorted(best.items(), key=lambda kv: -kv[1])
    return {"label": ranked[0][0], "agreement": round(ranked[0][1], 3),
            "margin": round(ranked[0][1] - (ranked[1][1] if len(ranked) > 1 else 0.0), 3)}


def _hits(found: Path, image: Path) -> list[dict]:
    data = json.loads(found.read_text(encoding="utf-8"))
    return data.get(str(image)) or next(iter(data.values()))


def _accept(spec: str | None) -> dict[int, str]:
    out = {}
    for part in (spec or "").split(","):
        if part.strip():
            i, _, label = part.partition(":")
            out[int(i)] = label or OTHER
    return out


def cmd_add(a) -> int:
    img = Image.open(a.image)
    hits = _hits(a.found, a.image)
    chosen = _accept(a.accept) | {i: lab for i, lab in _accept(a.reject).items()}
    entries = load_bank(a.bank)
    for i, label in chosen.items():
        h = hits[i]
        entries.append({"label": label, "map": a.map or a.image.stem, "bits": pack(thumbnail(img, h["x"], h["y"],
                        h.get("w", a.size), h.get("h", a.size)))})
    save_bank(a.bank, entries)
    print(f"added {len(chosen)} -> {a.bank} ({len(entries)} in the bank)")
    return 0


def cmd_harvest(a) -> int:
    img = Image.open(a.image)
    trace = json.loads(a.trace.read_text(encoding="utf-8"))
    pts = [r for r in trace if "point" in r and r.get("icon")]
    entries = load_bank(a.bank)
    for r in pts:
        entries.append({"label": r["icon"], "map": a.map or a.image.stem,
                        "bits": pack(thumbnail(img, r["point"][0], r["point"][1], a.size, a.size))})
    save_bank(a.bank, entries)
    print(f"harvested {len(pts)} pictograms from {a.trace.name} -> {a.bank} ({len(entries)} in the bank)")
    return 0


def cmd_classify(a) -> int:
    sys.path.insert(0, str(HERE))
    import detect_pictograms as dp

    bank = load_bank(a.bank)
    if not bank:
        print(f"the bank {a.bank} is empty: add pictograms first", file=sys.stderr)
        return 1
    img = Image.open(a.image)
    v = [int(t) for t in a.squares.split(",")]
    hits = dp.match_squares(img, tuple(v[:3]), v[3] if len(v) > 3 else 30, v[4] if len(v) > 4 else 14,
                            v[5] if len(v) > 5 else 48, a.min_x)
    if a.max_x:
        hits = [h for h in hits if h["x"] <= a.max_x]
    sure = []
    for i, h in enumerate(hits):
        c = classify(thumbnail(img, h["x"], h["y"], h.get("w", 34), h.get("h", 34)), bank)
        h.update(icon=c["label"], score=c["agreement"], margin=c["margin"])
        is_sure = c["agreement"] >= a.sure_agreement and c["margin"] >= a.sure_margin and c["label"] != OTHER
        h["sure"] = bool(is_sure)
        if is_sure:
            sure.append(f"{i}:{c['label']}")
        print(f"{i:>3}  ({h['x']:.0f},{h['y']:.0f})  {str(c['label']):<12} agreement {c['agreement']:.2f}  margin {c['margin']:.2f}"
              f"{'  sure' if is_sure else ''}")
    if a.write:
        a.write.write_text(json.dumps({str(a.image): hits}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{len(hits)} candidates, {len(sure)} sure. accept list for add_points: {','.join(sure)}")
    return 0


def cmd_loo(a) -> int:
    bank = load_bank(a.bank)
    if len(bank) < 2:
        print("too few in the bank", file=sys.stderr)
        return 1
    right = 0
    wrong = []
    for i, e in enumerate(bank):
        c = classify(unpack(e["bits"]), bank, exclude=i)
        right += c["label"] == e["label"]
        if c["label"] != e["label"]:
            wrong.append((e["label"], c["label"], e["map"], c["agreement"]))
    print(f"leave-one-out: {right}/{len(bank)} right ({100 * right / len(bank):.0f}%)")
    for w in wrong:
        print(f"  was {w[0]:<12} called {str(w[1]):<12} ({w[2]}, agreement {w[3]:.2f})")
    return 0


def cmd_list(a) -> int:
    bank = load_bank(a.bank)
    from collections import Counter

    print(f"{len(bank)} thumbnails in {a.bank}")
    for (label, mp), n in sorted(Counter((e["label"], e["map"]) for e in bank).items()):
        print(f"  {label:<14} {mp:<24} {n}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bank", type=Path, default=DEFAULT_BANK)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add")
    p.add_argument("image", type=Path)
    p.add_argument("found", type=Path)
    p.add_argument("--accept", help="index:label,... of the candidates that are facility pictograms")
    p.add_argument("--reject", help="index[:label],... of candidates that are not (label defaults to 'other')")
    p.add_argument("--map")
    p.add_argument("--size", type=float, default=34)
    p.set_defaults(func=cmd_add)
    p = sub.add_parser("harvest")
    p.add_argument("image", type=Path)
    p.add_argument("trace", type=Path)
    p.add_argument("--size", type=float, default=34, help="box side in pixels around each point (default 34)")
    p.add_argument("--map")
    p.set_defaults(func=cmd_harvest)
    p = sub.add_parser("classify")
    p.add_argument("image", type=Path)
    p.add_argument("--squares", required=True, metavar="R,G,B[,TOL[,MIN,MAX]]")
    p.add_argument("--min-x", type=int, default=0)
    p.add_argument("--max-x", type=int)
    p.add_argument("--sure-agreement", type=float, default=0.9)
    p.add_argument("--sure-margin", type=float, default=0.08)
    p.add_argument("--write", type=Path)
    p.set_defaults(func=cmd_classify)
    sub.add_parser("loo").set_defaults(func=cmd_loo)
    sub.add_parser("list").set_defaults(func=cmd_list)
    a = ap.parse_args()
    sys.exit(a.func(a))


if __name__ == "__main__":
    main()
