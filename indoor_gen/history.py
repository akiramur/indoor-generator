"""Snapshot history of built configs, used for undo.

Every successful build appends a snapshot (when the config changed).
Undo restores the previous snapshot into the config file.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def history_dir(config_path: Path, slug: str) -> Path:
    return config_path.resolve().parent / ".history" / slug


def _canonical(text: str) -> str:
    return json.dumps(json.loads(text), sort_keys=True, ensure_ascii=False)


def snapshots(config_path: Path, slug: str) -> list[Path]:
    d = history_dir(config_path, slug)
    return sorted(d.glob("*.json")) if d.exists() else []


def record(config_path: Path, slug: str) -> Path | None:
    """Store the current config as a snapshot unless identical to the latest."""
    text = config_path.read_text(encoding="utf-8")
    snaps = snapshots(config_path, slug)
    if snaps and _canonical(snaps[-1].read_text(encoding="utf-8")) == _canonical(text):
        return None
    d = history_dir(config_path, slug)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{len(snaps) + 1:04d}_{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
    path.write_text(text, encoding="utf-8")
    return path


def undo(config_path: Path, slug: str) -> Path:
    """Restore the previous built version. Returns the snapshot restored.

    If the config was edited but not successfully built, the last built
    snapshot is restored; otherwise the latest snapshot is dropped and the
    one before it is restored.
    """
    snaps = snapshots(config_path, slug)
    if not snaps:
        raise RuntimeError("no history to undo")
    current = _canonical(config_path.read_text(encoding="utf-8"))
    if _canonical(snaps[-1].read_text(encoding="utf-8")) == current:
        if len(snaps) < 2:
            raise RuntimeError("already at the first version")
        snaps[-1].unlink()
        target = snaps[-2]
    else:
        target = snaps[-1]
    config_path.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    return target
