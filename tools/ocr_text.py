"""Text lines (with their boxes) read from a raster floor map: `ocr_lines(image)`, or from the command line

    .venv/bin/python tools/ocr_text.py map.jpg [--out lines.json]

Engines, first that works: macOS Vision (tools/vision_ocr.swift, compiled once into ~/.cache/indoor-generator; reads
Japanese and English) or Tesseract (`tesseract`, with the `jpn` language data for Japanese). With neither, it says so.
Each line is {"text", "confidence", "x", "y", "w", "h"} in image pixels, origin top-left. The text is a candidate:
small errors (ディファレンス read as ティファレンス) are normal, so check names against the map.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = Path.home() / ".cache" / "indoor-generator"


class OcrUnavailable(RuntimeError):
    pass


def _vision_binary() -> Path | None:
    swift = HERE / "vision_ocr.swift"
    if sys.platform != "darwin" or not swift.exists() or shutil.which("swiftc") is None:
        return None
    binary = CACHE / "vision_ocr"
    if not binary.exists() or binary.stat().st_mtime < swift.stat().st_mtime:
        CACHE.mkdir(parents=True, exist_ok=True)
        done = subprocess.run(["swiftc", "-O", str(swift), "-o", str(binary)], capture_output=True, text=True)
        if done.returncode != 0:
            return None
    return binary


def _tesseract_lines(image: Path) -> list[dict]:
    langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True).stdout.split()
    lang = "jpn+eng" if "jpn" in langs else "eng"
    done = subprocess.run(["tesseract", str(image), "-", "-l", lang, "--psm", "11", "tsv"], capture_output=True, text=True)
    rows = [r.split("\t") for r in done.stdout.splitlines()[1:]]
    return [{"text": r[11], "confidence": float(r[10]) / 100, "x": float(r[6]), "y": float(r[7]), "w": float(r[8]),
             "h": float(r[9])} for r in rows if len(r) == 12 and r[11].strip() and float(r[10]) > 0]


def ocr_lines(image: Path) -> list[dict]:
    """Text lines of `image`, from macOS Vision when it is there, else Tesseract."""
    image = Path(image)
    binary = _vision_binary()
    if binary is not None:
        done = subprocess.run([str(binary), str(image)], capture_output=True, text=True)
        if done.returncode == 0:
            return json.loads(done.stdout)
    if shutil.which("tesseract"):
        return _tesseract_lines(image)
    raise OcrUnavailable("no OCR engine: needs macOS (swiftc, for Vision) or tesseract")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", type=Path)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    lines = ocr_lines(a.image)
    text = json.dumps(lines, ensure_ascii=False, indent=1)
    if a.out:
        a.out.write_text(text, encoding="utf-8")
        print(f"{len(lines)} lines -> {a.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
