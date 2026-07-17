"""Library management + folder browser + background rescan.

Split out of api.py; shared infrastructure (DB proxy, auth deps, caches) is
imported from there — api.py includes this router at its bottom, after those
names exist.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

# api.py includes this router at its very bottom, so by the time this module is
# imported the shared names below already exist — even though api is technically
# still mid-import (a "partial" module). Attribute access like api._db happens
# at request time, when it's fully initialized.
import api
from api import require_token, require_token_read

router = APIRouter()


def _api():
    """The shared api module (fully initialized once requests flow)."""
    return api


# --------------------------------------------------------- library management --

@router.get("/api/libraries", dependencies=[Depends(require_token_read)])
def list_libraries() -> list[dict[str, Any]]:
    """Configured libraries with name, privacy/default flags, reachability, and
    series count — feeds the settings menu and the floating switcher."""
    api = _api()
    out: list[dict[str, Any]] = []
    for lib in api._db.get_libraries():
        p = str(lib.get("path") or "")
        try:
            online = Path(p).exists()
        except OSError:
            online = False
        lid = lib.get("id")
        count = len(api._db.get_series_list(library_id=lid)) if lid is not None else 0
        out.append({
            "id": lid,
            "path": p,
            "name": lib.get("name"),
            "private": bool(lib.get("private")),
            "is_default": bool(lib.get("is_default")),
            "online": online,
            "count": count,
        })
    return out


class LibraryUpdateIn(BaseModel):
    name: str | None = None
    private: bool | None = None


@router.post("/api/libraries/{library_id}", dependencies=[Depends(require_token)])
def update_library(library_id: int, body: LibraryUpdateIn) -> dict[str, Any]:
    """Rename a library or toggle its private flag."""
    api = _api()
    name = None if body.name is None else api._sanitize_name(body.name) or "Library"
    api._db.update_library(library_id, name=name, private=body.private)
    return {"ok": True, "libraries": list_libraries()}


@router.post("/api/libraries/{library_id}/default", dependencies=[Depends(require_token)])
def set_default_library(library_id: int) -> dict[str, Any]:
    """Mark this library as the one the app opens to (when that setting is on)."""
    api = _api()
    api._db.set_default_library(library_id)
    return {"ok": True, "libraries": list_libraries()}


@router.delete("/api/libraries/{library_id}", dependencies=[Depends(require_token)])
def remove_library(library_id: int) -> dict[str, Any]:
    """Remove a library (and its series records) from the APP ONLY — nothing on
    disk is deleted; re-adding the folder later rescans it back. Refuses to remove
    the last remaining library (the app needs at least one)."""
    api = _api()
    libs = api._db.get_libraries()
    if not any(int(l.get("id", -1)) == library_id for l in libs):
        raise HTTPException(status_code=404, detail="Library not found")
    if len(libs) <= 1:
        raise HTTPException(status_code=400, detail="Can't remove the last library.")
    removed = api._db.remove_library(library_id)
    # Drop caches that may reference the removed series so nothing stale lingers.
    with api._CACHE_LOCK:
        api._COVER_PATH_CACHE.clear()
    api._invalidate_chapters_cache()
    return {"ok": True, "removed_series": removed, "libraries": list_libraries()}


@router.get("/api/browse", dependencies=[Depends(require_token_read)])
def browse_dirs(path: str = Query(default="")) -> dict[str, Any]:
    """List subdirectories of `path` so the UI can pick a library folder. With no
    path, returns the available drive/filesystem roots. Read-only; lists folders
    only (never file contents), so there's no data exposure beyond folder names."""
    # No path → enumerate roots. On Windows that's the mounted drive letters.
    if not path:
        roots: list[dict[str, Any]] = []
        if os.name == "nt":
            import string
            for letter in string.ascii_uppercase:
                drive = f"{letter}:\\"
                if os.path.exists(drive):
                    roots.append({"name": drive, "path": drive})
        else:
            roots.append({"name": "/", "path": "/"})
        return {"path": "", "parent": None, "dirs": roots}

    p = Path(path)
    try:
        if not p.exists() or not p.is_dir():
            raise HTTPException(status_code=404, detail="Folder not found")
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    dirs: list[dict[str, Any]] = []
    try:
        for entry in sorted(p.iterdir(), key=lambda e: e.name.lower()):
            try:
                if entry.is_dir() and not entry.name.startswith("."):
                    dirs.append({"name": entry.name, "path": str(entry)})
            except OSError:
                continue  # permission denied on a single entry — skip it
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Cannot read folder: {exc}")

    parent = str(p.parent) if p.parent != p else None
    return {"path": str(p), "parent": parent, "dirs": dirs}


# ------------------------------------------------------------------- rescan --
# Background rescan state. One scan at a time; the UI polls /api/rescan/status.
_RESCAN_LOCK = threading.Lock()
_RESCAN_STATE: dict[str, Any] = {"running": False, "done": False, "error": None, "added": None, "series": 0}


def _run_rescan(add_path: str | None, add_name: str | None = None,
                library_id: int | None = None) -> None:
    api = _api()
    try:
        if add_path:
            # Adding a new folder always scans just that new library.
            new_id = api._db.add_library(add_path, name=add_name)
            library_id = new_id
        from scanner import scan_and_sync
        result = scan_and_sync(api._db, library_id=library_id)
        total = sum(len(v) for v in (result or {}).values())
        with _RESCAN_LOCK:
            _RESCAN_STATE.update(running=False, done=True, error=None, series=total)
        # New/renamed folders invalidate cached covers, counts, and chapters.
        with api._CACHE_LOCK:
            api._COVER_PATH_CACHE.clear()
        api._invalidate_chapters_cache()   # all of them — folders may have changed
    except Exception as exc:  # noqa: BLE001
        # Full traceback to the server log — a bare str(exc) like "FOREIGN KEY
        # constraint failed" gives no line number, making scan failures
        # near-impossible to diagnose after the fact.
        import traceback
        traceback.print_exc()
        with _RESCAN_LOCK:
            _RESCAN_STATE.update(running=False, done=True, error=str(exc))


class RescanIn(BaseModel):
    add_path: str | None = None   # optional new library folder to add before scanning
    add_name: str | None = None   # display name for the new library (defaults to folder basename)
    library_id: int | None = None # scan ONLY this existing library; omit to scan all


@router.post("/api/rescan", dependencies=[Depends(require_token)])
def rescan(body: RescanIn) -> dict[str, Any]:
    """Kick off a disk rescan in the background. With `library_id`, scans only that
    library; with `add_path`, adds + scans a new library; otherwise scans all.
    Returns immediately; poll /api/rescan/status for progress."""
    add_path = (body.add_path or "").strip() or None
    add_name = (body.add_name or "").strip() or None
    library_id = body.library_id
    if add_path:
        p = Path(add_path)
        if not p.exists() or not p.is_dir():
            raise HTTPException(status_code=400, detail="Folder does not exist")
    with _RESCAN_LOCK:
        if _RESCAN_STATE["running"]:
            raise HTTPException(status_code=409, detail="A rescan is already running")
        _RESCAN_STATE.update(running=True, done=False, error=None, added=add_path, series=0)
    threading.Thread(target=_run_rescan, args=(add_path, add_name, library_id), name="rescan", daemon=True).start()
    return {"ok": True, "running": True}


@router.get("/api/rescan/status", dependencies=[Depends(require_token_read)])
def rescan_status() -> dict[str, Any]:
    with _RESCAN_LOCK:
        return dict(_RESCAN_STATE)
