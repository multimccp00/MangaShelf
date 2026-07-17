"""Background jobs: the import/re-sync queue, series moves, resync endpoints.

Split out of api.py; shared infrastructure is imported from there (api.py
includes this router at its bottom — see routes/__init__).
"""
from __future__ import annotations

import threading
import os as _os
import json as _json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import api
from api import require_token, require_token_read
from routes.sources_ext import _library_path
from sources.registry import (
    find_source, list_sources, find_by_name, source_info,
    install_extension, remove_extension, set_extension_enabled, list_extensions,
    search_all, any_searchable,
)
from sources.downloader import download_series

router = APIRouter()


# ------------------------------------------------------------- import queue --
# Imports and re-syncs are queued and run one at a time by a single worker
# thread, so you can line several up. The UI polls /api/scrape/status for the
# active job + how many are waiting. (Endpoint name kept for compatibility.)
_JOB_LOCK = threading.Lock()
_JOB_CV = threading.Condition(_JOB_LOCK)
_JOBS: list[dict[str, Any]] = []       # queued + running + recently finished
_JOB_NEXT_ID = 1
_JOB_WORKER_STARTED = False

# Persist the import queue to disk so it SURVIVES A SERVER RESTART: a job that was
# queued/running when the server stopped is re-queued on startup (the download
# itself is resumable — files already on disk are skipped). Without this, a restart
# mid-import loses the queue + its progress indicator entirely.
_JOBS_PATH = Path.home() / ".mangashelf" / "web_import_queue.json"
# Only these fields are needed to reconstruct/resume a job (progress/message are
# transient and rebuilt as it runs).
_JOB_PERSIST_KEYS = ("id", "kind", "title", "url", "library_id", "library_root",
                     "source", "folder")


def _save_jobs() -> None:
    """Persist queued + running jobs (finished ones don't need to survive)."""
    try:
        with _JOB_LOCK:
            keep = [{k: j.get(k) for k in _JOB_PERSIST_KEYS}
                    for j in _JOBS if j["status"] in ("queued", "running")]
        tmp = _JOBS_PATH.with_suffix(".json.tmp")
        _JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(_json.dumps(keep), encoding="utf-8")
        _os.replace(tmp, _JOBS_PATH)
    except Exception:
        pass


def _restore_jobs() -> None:
    """On startup, re-queue any jobs that were in flight when the server stopped."""
    global _JOB_NEXT_ID, _JOB_WORKER_STARTED
    try:
        if not _JOBS_PATH.exists():
            return
        saved = _json.loads(_JOBS_PATH.read_text(encoding="utf-8")) or []
    except Exception:
        return
    if not isinstance(saved, list) or not saved:
        return
    with _JOB_CV:
        for s in saved:
            if not isinstance(s, dict) or not s.get("kind"):
                continue
            job = {
                "id": _JOB_NEXT_ID, "status": "queued",
                "chapter": 0, "total": 0, "pages": 0,
                "message": "Resuming after restart…", "error": None, "done_message": "Done.",
                **{k: s.get(k) for k in _JOB_PERSIST_KEYS if k != "id"},
            }
            _JOB_NEXT_ID += 1
            _JOBS.append(job)
        if _JOBS and not _JOB_WORKER_STARTED:
            _JOB_WORKER_STARTED = True
            threading.Thread(target=_job_worker, name="import-queue", daemon=True).start()
        _JOB_CV.notify_all()


def _job_set(job: dict[str, Any], **fields: Any) -> None:
    with _JOB_LOCK:
        job.update(fields)


def _enqueue_job(kind: str, **fields: Any) -> dict[str, Any]:
    global _JOB_NEXT_ID, _JOB_WORKER_STARTED
    with _JOB_CV:
        job = {
            "id": _JOB_NEXT_ID, "kind": kind, "status": "queued",
            "title": fields.get("title") or "", "chapter": 0, "total": 0,
            "pages": 0, "message": "Queued…", "error": None, "done_message": "Done.",
            **fields,
        }
        _JOB_NEXT_ID += 1
        _JOBS.append(job)
        if not _JOB_WORKER_STARTED:
            _JOB_WORKER_STARTED = True
            threading.Thread(target=_job_worker, name="import-queue", daemon=True).start()
        _JOB_CV.notify_all()
    _save_jobs()   # persist so a restart mid-queue doesn't lose this job
    return job


def _execute_job(job: dict[str, Any], scan_and_sync) -> None:
    def progress(done_ch: int, total_ch: int, label: str, new_pages: int) -> None:
        _job_set(job, chapter=done_ch, total=total_ch, pages=new_pages,
                 message=f"{label}  ({done_ch}/{total_ch})")

    if job["kind"] == "resync":
        src = find_by_name(job["source"])
        if not src:
            raise ValueError(f"The source adapter for '{job['source']}' isn't available.")
        # Was the reader caught up BEFORE this sync (read to the end of what was on
        # disk)? If so and the sync brings new pages, flag the series so Continue
        # Reading bumps it to the front — "a series you finished just got more".
        caught_up = False
        sid_before = api._db.series_id_for_folder(job["folder"])
        if sid_before:
            row_before = api._db.get_series_by_id(sid_before)
            with api._CACHE_LOCK:
                total_before = api._PAGE_COUNTS.get(str(sid_before))
            if row_before and total_before:
                caught_up = int(row_before.get("last_page") or 0) >= int(total_before)
        _job_set(job, message="Checking for new chapters…")
        meta = src.fetch_series(job["url"])
        _job_set(job, title=meta.title, total=len(meta.chapters),
                 message=f"Syncing {len(meta.chapters)} chapters…")
        result = download_series(src, meta, str(Path(job["folder"]).parent),
                                 progress=progress, dest_dir=job["folder"])
        new_pages = int(result.get("new_pages", 0))
        job["done_message"] = f"{new_pages} new pages." if new_pages else "Up to date."
        if new_pages and sid_before:
            if caught_up:
                try:
                    api._db.set_fresh_chapters(sid_before, True)
                except Exception as exc:  # noqa: BLE001
                    print(f"[resync] fresh-chapters flag failed: {exc}")
            # The cached page total is now stale (new pages on disk). Drop it so
            # the next library listing re-counts (via the background warm-up)
            # instead of showing the old total.
            with api._CACHE_LOCK:
                api._PAGE_COUNTS.pop(str(sid_before), None)
            api._save_page_counts()
    else:  # import
        src = find_source(job["url"])
        if not src:
            raise ValueError("No import source recognizes this link.")
        _job_set(job, message="Reading series…")
        meta = src.fetch_series(job["url"])
        # A source can resolve chapters from a DIFFERENT adapter (e.g. AniList hands
        # off to MangaDex for readable pages). Download pages using the adapter that
        # actually owns the chapters (meta.source), falling back to the URL adapter.
        page_src = find_by_name(meta.source) or src
        _job_set(job, title=meta.title, total=len(meta.chapters),
                 message=f"Downloading {len(meta.chapters)} chapters…")
        result = download_series(page_src, meta, job["library_root"], progress=progress)
        job["done_message"] = f"Imported “{meta.title}”."

    # Note any pages/chapters that couldn't be downloaded, so it isn't silently
    # reported as a clean success.
    fp = int(result.get("failed_pages", 0))
    fc = int(result.get("failed_chapters", 0))
    if fp or fc:
        bits = []
        if fp:
            bits.append(f"{fp} page{'s' if fp != 1 else ''}")
        if fc:
            bits.append(f"{fc} chapter{'s' if fc != 1 else ''}")
        job["done_message"] += f"  ({' + '.join(bits)} failed)"
        job["warning"] = True

    _job_set(job, message="Scanning into library…")
    scan_and_sync(api._db, library_id=job["library_id"])
    # For a fresh import, save the source's real cover as the series cover.
    if job["kind"] != "resync":
        try:
            sid = api._db.series_id_for_folder(result["series_dir"])
            if sid and getattr(meta, "cover_url", ""):
                api._save_source_cover(sid, meta.cover_url, src.image_headers())
        except Exception as exc:  # noqa: BLE001
            print(f"[cover] post-import cover step failed: {exc}")
    with api._CACHE_LOCK:
        api._COVER_PATH_CACHE.clear()
    api._invalidate_chapters_cache()


def _job_worker() -> None:
    from scanner import scan_and_sync
    while True:
        with _JOB_CV:
            job = next((j for j in _JOBS if j["status"] == "queued"), None)
            while job is None:
                _JOB_CV.wait()
                job = next((j for j in _JOBS if j["status"] == "queued"), None)
            job["status"] = "running"
            job["message"] = "Starting…"
        _save_jobs()   # mark running (survives a restart mid-download)
        try:
            _execute_job(job, scan_and_sync)
            with _JOB_LOCK:
                if not job["error"]:
                    job["status"] = "done"
                    job["message"] = job["done_message"]
        except Exception as exc:  # noqa: BLE001
            with _JOB_LOCK:
                job["status"] = "error"
                job["error"] = str(exc)
        # Cap retained finished jobs so the list can't grow without bound.
        with _JOB_LOCK:
            finished = [j for j in _JOBS if j["status"] in ("done", "error")]
            for old in finished[:-10]:
                _JOBS.remove(old)
        _save_jobs()   # a finished job drops out of the persisted (queued/running) set


# Rehydrate the queue from disk now that the worker fn exists — re-queues any
# import that was in flight when the server last stopped.
_restore_jobs()


def _scrape_status() -> dict[str, Any]:
    with _JOB_LOCK:
        active = next((j for j in _JOBS if j["status"] == "running"), None)
        queued = [j for j in _JOBS if j["status"] == "queued"]
        display = active
        if display is None:
            finished = [j for j in _JOBS if j["status"] in ("done", "error")]
            display = finished[-1] if finished else None
        out: dict[str, Any] = {
            "running": active is not None or len(queued) > 0,
            "queued_count": len(queued),
            "queued": [{"id": j["id"], "title": j["title"] or "Queued"} for j in queued],
        }
        if display:
            out.update(id=display["id"], title=display["title"], kind=display["kind"],
                       chapter=display["chapter"], total=display["total"],
                       pages=display["pages"], message=display["message"],
                       error=display["error"], done=display["status"] in ("done", "error"),
                       warning=bool(display.get("warning")))
        else:
            out.update(id=None, title=None, kind=None, chapter=0, total=0, pages=0,
                       message="", error=None, done=True)
        return out


class ScrapeIn(BaseModel):
    url: str
    library_id: int
    title: str | None = None   # optional: previewed title, so the queue can name it


@router.post("/api/scrape", dependencies=[Depends(require_token)])
def scrape(body: ScrapeIn) -> dict[str, Any]:
    """Queue a series import. Runs in the background behind any earlier jobs;
    poll /api/scrape/status for the active job + queue length."""
    url = (body.url or "").strip()
    if not find_source(url):
        raise HTTPException(status_code=400, detail="No import source recognizes this link.")
    root = _library_path(body.library_id)
    if not root:
        raise HTTPException(status_code=400, detail="Unknown target library.")
    if not Path(root).is_dir():
        raise HTTPException(status_code=400, detail="Target library folder is offline.")
    job = _enqueue_job("import", url=url, library_id=body.library_id, library_root=root,
                       title=(body.title or "").strip())
    with _JOB_LOCK:
        queued = sum(1 for j in _JOBS if j["status"] == "queued")
    return {"ok": True, "id": job["id"], "queued": queued}


@router.get("/api/scrape/status", dependencies=[Depends(require_token_read)])
def scrape_status() -> dict[str, Any]:
    return _scrape_status()


@router.delete("/api/scrape/{job_id}", dependencies=[Depends(require_token)])
def cancel_job(job_id: int) -> dict[str, Any]:
    """Remove a QUEUED import/re-sync job. A job that's already running can't be
    cancelled this way (it finishes)."""
    removed = False
    with _JOB_LOCK:
        for j in _JOBS:
            if j["id"] == job_id and j["status"] == "queued":
                _JOBS.remove(j)
                removed = True
                break
    if removed:
        _save_jobs()
        return {"ok": True}
    raise HTTPException(status_code=404, detail="No queued job with that id (it may have already started).")


# ------------------------------------------- move a series to another library --
# A library is a folder on disk, so moving a series between libraries means
# physically moving its folder to the target library's root, then repointing the
# DB row. Done safely: copy -> verify -> update DB -> delete source. If anything
# fails before the DB is updated, the partial target is removed and the original
# is left untouched (a failed move is a no-op).
# Moves run one at a time (safe for disk) but queue up, in their own lane
# independent of imports. Same queue machinery + status shape as imports, so the
# UI's move indicator gets the queue popover + cancel for free.
_MOVE_LOCK = threading.Lock()
_MOVE_CV = threading.Condition(_MOVE_LOCK)
_MOVE_JOBS: list[dict[str, Any]] = []
_MOVE_NEXT_ID = 1
_MOVE_WORKER_STARTED = False


def _dir_manifest(root: Path):
    files, total = [], 0
    for p in root.rglob("*"):
        if p.is_file():
            files.append(p.relative_to(root))
            total += p.stat().st_size
    return files, total


def _mv_set(job: dict[str, Any], **fields: Any) -> None:
    with _MOVE_LOCK:
        job.update(fields)


def _enqueue_move(**fields: Any) -> dict[str, Any]:
    global _MOVE_NEXT_ID, _MOVE_WORKER_STARTED
    with _MOVE_CV:
        job = {"id": _MOVE_NEXT_ID, "kind": "move", "status": "queued",
               "title": fields.get("title") or "", "copied": 0, "total": 0,
               "message": "Queued…", "error": None, "done_message": "Moved.", **fields}
        _MOVE_NEXT_ID += 1
        _MOVE_JOBS.append(job)
        if not _MOVE_WORKER_STARTED:
            _MOVE_WORKER_STARTED = True
            threading.Thread(target=_move_worker, name="move-queue", daemon=True).start()
        _MOVE_CV.notify_all()
        return job


def _execute_move(job: dict[str, Any]) -> None:
    import shutil
    src, dst = Path(job["src_path"]), Path(job["target_path"])
    committed = False
    try:
        files, total_bytes = _dir_manifest(src)
        _mv_set(job, total=len(files), message="Copying…")
        copied = 0
        for rel in files:
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, dst / rel)
            copied += 1
            if copied % 25 == 0:
                _mv_set(job, copied=copied)
        vfiles, vbytes = _dir_manifest(dst)
        if len(vfiles) != len(files) or vbytes != total_bytes:
            raise RuntimeError("copy verification failed (file count / size mismatch)")
        api._db.set_series_location(job["series_id"], job["target_lib_id"], str(dst))
        committed = True
        shutil.rmtree(src, ignore_errors=True)
        with api._CACHE_LOCK:
            api._COVER_PATH_CACHE.clear()
        api._invalidate_chapters_cache()
        _mv_set(job, copied=copied)
    except Exception as exc:  # noqa: BLE001
        if not committed:
            shutil.rmtree(job["target_path"], ignore_errors=True)  # remove partial copy
        raise RuntimeError(f"Move failed (original left intact): {exc}")


def _move_worker() -> None:
    while True:
        with _MOVE_CV:
            job = next((j for j in _MOVE_JOBS if j["status"] == "queued"), None)
            while job is None:
                _MOVE_CV.wait()
                job = next((j for j in _MOVE_JOBS if j["status"] == "queued"), None)
            job["status"] = "running"
            job["message"] = "Starting…"
        try:
            _execute_move(job)
            with _MOVE_LOCK:
                job["status"] = "done"
                job["message"] = job["done_message"]
        except Exception as exc:  # noqa: BLE001
            with _MOVE_LOCK:
                job["status"] = "error"
                job["error"] = str(exc)
        with _MOVE_LOCK:
            finished = [j for j in _MOVE_JOBS if j["status"] in ("done", "error")]
            for old in finished[:-10]:
                _MOVE_JOBS.remove(old)


def _move_status() -> dict[str, Any]:
    with _MOVE_LOCK:
        active = next((j for j in _MOVE_JOBS if j["status"] == "running"), None)
        queued = [j for j in _MOVE_JOBS if j["status"] == "queued"]
        display = active or (([j for j in _MOVE_JOBS if j["status"] in ("done", "error")] or [None])[-1])
        out: dict[str, Any] = {
            "running": active is not None or len(queued) > 0,
            "queued_count": len(queued),
            "queued": [{"id": j["id"], "title": j["title"] or "Move"} for j in queued],
        }
        if display:
            out.update(id=display["id"], title=display["title"], kind="move",
                       copied=display["copied"], total=display["total"],
                       message=display["message"], error=display["error"],
                       done=display["status"] in ("done", "error"))
        else:
            out.update(id=None, title=None, kind="move", copied=0, total=0,
                       message="", error=None, done=True)
        return out


class MoveIn(BaseModel):
    library_id: int


@router.post("/api/series/{series_id}/move", dependencies=[Depends(require_token)])
def move_series(series_id: int, body: MoveIn) -> dict[str, Any]:
    data = api._db.get_series_by_id(series_id)
    if not data:
        raise HTTPException(status_code=404, detail="Series not found")
    src_path = str(data.get("folder_path") or "")
    if not src_path or not Path(src_path).is_dir():
        raise HTTPException(status_code=400, detail="Series folder not found on disk.")
    if data.get("library_id") == body.library_id:
        raise HTTPException(status_code=400, detail="Series is already in that library.")
    target_root = _library_path(body.library_id)
    if not target_root:
        raise HTTPException(status_code=400, detail="Unknown target library.")
    if not Path(target_root).is_dir():
        raise HTTPException(status_code=400, detail="Target library folder is offline.")
    target_path = str(Path(target_root) / Path(src_path).name)
    if Path(target_path).exists():
        raise HTTPException(status_code=400, detail="A folder with that name already exists in the target library.")
    # Guard against queuing the same series' move twice.
    with _MOVE_LOCK:
        if any(j["series_id"] == series_id and j["status"] in ("queued", "running") for j in _MOVE_JOBS):
            raise HTTPException(status_code=409, detail="A move for this series is already queued.")
    job = _enqueue_move(series_id=series_id, target_lib_id=body.library_id,
                        src_path=src_path, target_path=target_path,
                        title=data.get("title") or "")
    with _MOVE_LOCK:
        queued = sum(1 for j in _MOVE_JOBS if j["status"] == "queued")
    return {"ok": True, "id": job["id"], "queued": queued}


@router.get("/api/move/status", dependencies=[Depends(require_token_read)])
def move_status() -> dict[str, Any]:
    return _move_status()


@router.delete("/api/move/{job_id}", dependencies=[Depends(require_token)])
def cancel_move(job_id: int) -> dict[str, Any]:
    """Remove a QUEUED move. A running move can't be cancelled (it finishes)."""
    with _MOVE_LOCK:
        for j in _MOVE_JOBS:
            if j["id"] == job_id and j["status"] == "queued":
                _MOVE_JOBS.remove(j)
                return {"ok": True}
    raise HTTPException(status_code=404, detail="No queued move with that id.")


# ------------------------------------------ re-sync a series from its origin --
@router.post("/api/series/{series_id}/resync", dependencies=[Depends(require_token)])
def resync_series(series_id: int) -> dict[str, Any]:
    """Queue a re-sync of this series from its origin (downloads any new
    chapters). Shares the import queue; poll /api/scrape/status for progress."""
    data = api._db.get_series_by_id(series_id)
    if not data:
        raise HTTPException(status_code=404, detail="Series not found")
    info = source_info(str(data.get("external_id") or ""))
    if not info or not info.get("url"):
        raise HTTPException(status_code=400, detail="This series has no known source to update from.")
    if not find_by_name(info["source"]):
        raise HTTPException(status_code=400, detail=f"The source adapter for '{info['source']}' isn't available on this machine.")
    folder = str(data.get("folder_path") or "")
    if not folder or not Path(folder).is_dir():
        raise HTTPException(status_code=400, detail="Series folder not found on disk.")
    job = _enqueue_job("resync", url=info["url"], source=info["source"],
                       library_id=data.get("library_id"), folder=folder,
                       title=data.get("title") or "")
    with _JOB_LOCK:
        queued = sum(1 for j in _JOBS if j["status"] == "queued")
    return {"ok": True, "id": job["id"], "queued": queued}


@router.post("/api/resync-all", dependencies=[Depends(require_token)])
def resync_all(library: int | None = None) -> dict[str, Any]:
    """Queue a re-sync for EVERY imported series in the library (those with a
    known origin) — the one-click "check everything for new chapters". Series
    whose adapter/folder is unavailable are skipped, as are ones already in the
    queue. Jobs run one at a time on the existing import worker; the UI's usual
    job indicator shows progress."""
    rows = api._db.get_series_list(library_id=library)
    with _JOB_LOCK:
        pending_folders = {str(j.get("folder") or "")
                           for j in _JOBS if j["status"] in ("queued", "running")}
    queued = 0
    skipped = 0
    for data in rows:
        info = source_info(str(data.get("external_id") or ""))
        folder = str(data.get("folder_path") or "")
        if (not info or not info.get("url") or not find_by_name(info["source"])
                or not folder or folder in pending_folders or not Path(folder).is_dir()):
            skipped += 1
            continue
        _enqueue_job("resync", url=info["url"], source=info["source"],
                     library_id=data.get("library_id"), folder=folder,
                     title=data.get("title") or "")
        queued += 1
    return {"ok": True, "queued": queued, "skipped": skipped}


