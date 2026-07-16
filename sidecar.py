from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SIDECAR_FILENAME = "metadata.json"


def sidecar_path(folder_path: str | Path) -> Path:
    return Path(folder_path) / SIDECAR_FILENAME


def read_sidecar(folder_path: str | Path) -> dict[str, Any] | None:
    path = sidecar_path(folder_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    # Legacy key migration: expose the old key under its current name.
    if isinstance(data, dict) and "nhentai_id" in data:
        data.setdefault("external_id", data.pop("nhentai_id"))
    return data


def write_sidecar(folder_path: str | Path, data: dict[str, Any]) -> None:
    """Write metadata.json into folder_path.  Only writes keys we own; ignores extras."""
    path = sidecar_path(folder_path)
    # Merge with existing sidecar so we never lose keys written by other tools.
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    # Legacy key migration: fold any old key into external_id, then drop it so it
    # doesn't linger in the rewritten sidecar.
    if "nhentai_id" in existing:
        existing.setdefault("external_id", existing.pop("nhentai_id"))

    existing.update({
        "title": str(data.get("title", Path(folder_path).name)).strip() or Path(folder_path).name,
        "series": str(data.get("series", existing.get("series", ""))).strip(),
        "external_id": data.get("external_id", existing.get("external_id")),
        "author": str(data.get("author", existing.get("author", ""))).strip(),
        "language": str(data.get("language", existing.get("language", ""))).strip(),
        "status": str(data.get("status", existing.get("status", "Not Started"))),
        "rating": int(data.get("rating", existing.get("rating", 0))),
        "favorite": bool(data.get("favorite", existing.get("favorite", False))),
        "notes": str(data.get("notes", existing.get("notes", ""))).strip(),
        "tags": sorted({str(t).strip() for t in data.get("tags", existing.get("tags", [])) if str(t).strip()}),
        "genres": sorted({str(g).strip() for g in data.get("genres", existing.get("genres", [])) if str(g).strip()}),
        "parodies": sorted({str(p).strip() for p in data.get("parodies", existing.get("parodies", [])) if str(p).strip()}),
        "last_chapter": data.get("last_chapter", existing.get("last_chapter")),
        "last_read": data.get("last_read", existing.get("last_read")),
    })

    # Write atomically: serialize to a temp file in the same directory, then
    # os.replace() it onto the target. A crash/power-loss mid-write then leaves
    # either the old complete file or the new complete file — never a truncated
    # one that read_sidecar would discard (losing all curated metadata).
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
