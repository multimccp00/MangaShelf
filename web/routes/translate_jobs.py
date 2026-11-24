"""Machine-translation queue: raw chapters -> translation.en.json, run while the PC is idle.

Separate from the import queue so a long translation never blocks imports, and
so it can sit waiting for idle time without holding anything else up.
"""
from __future__ import annotations

import json as _json
import os as _os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import api
import translate as mt
from api import require_token, require_token_read

router = APIRouter()

_LOCK = threading.Lock()
_CV = threading.Condition(_LOCK)
_Q: list[dict[str, Any]] = []
_NEXT = 1
_STARTED = False
_PATH = Path.home() / ".mangashelf" / "mt_queue.json"
_KEEP = ("series_id", "title", "chapter", "path", "lang", "now")


def _settings() -> dict[str, Any]:
    st = api._load_settings()
    return {"model": str(st.get("mtModel") or mt.DEFAULT_MODEL),
            "auto": bool(st.get("mtAuto", True)),
            "idle": max(0, int(st.get("mtIdleMinutes", 10) or 0)) * 60}


def _save() -> None:
    try:
        with _LOCK:
            keep = [{k: j.get(k) for k in _KEEP} for j in _Q if j["status"] in ("queued", "running", "waiting")]
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PATH.with_suffix(".tmp")
        tmp.write_text(_json.dumps(keep), encoding="utf-8")
        _os.replace(tmp, _PATH)
    except Exception:
        pass


def _ensure_worker() -> None:
    global _STARTED
    if not _STARTED:
        _STARTED = True
        threading.Thread(target=_worker, name="mt-queue", daemon=True).start()


def _enqueue(series_id: int, title: str, chapter: str, path: str, lang: str, now: bool) -> bool:
    global _NEXT
    with _CV:
        for j in _Q:
            if j["path"] == path and j["status"] in ("queued", "running", "waiting"):
                if now and not j.get("now"):
                    j["now"] = True          # promote: user wants it read now
                    _CV.notify_all()
                return False
        _Q.append({"id": _NEXT, "series_id": series_id, "title": title, "chapter": chapter,
                   "path": path, "lang": lang, "now": now, "status": "queued",
                   "message": "Queued", "error": None})
        _NEXT += 1
        _ensure_worker()
        _CV.notify_all()
    _save()
    return True


def raw_chapters(series_id: int, only_missing: bool = True) -> list[dict[str, Any]]:
    data = api._db.get_series_by_id(series_id)
    if not data:
        return []
    out = []
    for ch in api._chapters_payload(str(data["folder_path"])):
        if (ch.get("language") or "").lower() in mt.RAW_LANGS and not (only_missing and ch.get("translated")):
            out.append(ch)
    return out


def enqueue_series(series_id: int, now: bool = False) -> int:
    """Queue every raw, not-yet-translated chapter of a series. Used after imports
    and re-syncs (when auto-translate is on) and by the "translate all" button."""
    data = api._db.get_series_by_id(series_id)
    if not data or not mt.ocr_available():
        return 0
    n = 0
    for ch in raw_chapters(series_id):
        n += _enqueue(series_id, str(data.get("title") or ""), ch["name"], ch["path"], ch["language"], now)
    return n


def auto_enqueue(series_id: int) -> None:
    if _settings()["auto"]:
        try:
            enqueue_series(series_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[mt] auto-queue failed: {exc}")


def _set(job: dict[str, Any], **kw: Any) -> None:
    with _LOCK:
        job.update(kw)


def _idle_gate(job: dict[str, Any], check_gpu: bool = False) -> None:
    """Block until the PC has been idle long enough (skipped for "now" jobs)."""
    while True:
        with _LOCK:
            if job not in _Q:
                raise RuntimeError("Cancelled")
        need = _settings()["idle"]
        if job.get("now") or need <= 0:
            return
        idle = mt.idle_seconds()
        if idle >= need and not (check_gpu and mt.gpu_busy()):
            if job["status"] == "waiting":
                _set(job, status="running")
            return
        _set(job, status="waiting",
             message="Waiting for the GPU to be free" if idle >= need else
             f"Waiting until the PC is idle ({int(need // 60)} min)")
        with _CV:
            _CV.wait(timeout=20)     # woken early when a job is promoted to "now"


def _worker() -> None:
    while True:
        with _CV:
            pending = [j for j in _Q if j["status"] == "queued"]
            while not pending:
                _CV.wait()
                pending = [j for j in _Q if j["status"] == "queued"]
            job = next((j for j in pending if j.get("now")), pending[0])
            job["status"] = "running"
        _save()
        try:
            ch_dir = Path(job["path"])
            if not ch_dir.is_dir():
                raise FileNotFoundError("Chapter folder is gone.")
            _idle_gate(job, check_gpu=True)
            n = mt.translate_chapter(ch_dir, job["lang"], _settings()["model"],
                                     wait=lambda: _idle_gate(job),
                                     progress=lambda m: _set(job, message=m))
            _set(job, status="done", message=f"{n} lines translated")
            api._invalidate_chapters_cache(str(ch_dir.parent))
        except Exception as exc:  # noqa: BLE001
            _set(job, status="error", error=str(exc), message="Failed")
            print(f"[mt] {job['title']} / {job['chapter']}: {exc}")
        with _LOCK:   # keep only the last few finished jobs around for the status view
            done = [j for j in _Q if j["status"] in ("done", "error")]
            for old in done[:-10]:
                _Q.remove(old)
        _save()


def _restore() -> None:
    try:
        saved = _json.loads(_PATH.read_text(encoding="utf-8")) if _PATH.exists() else []
    except Exception:
        saved = []
    for s in saved if isinstance(saved, list) else []:
        if isinstance(s, dict) and s.get("path"):
            _enqueue(int(s.get("series_id") or 0), str(s.get("title") or ""), str(s.get("chapter") or ""),
                     str(s["path"]), str(s.get("lang") or ""), bool(s.get("now")))


_restore()


# ----------------------------------------------------------------- routes --
class TranslateIn(BaseModel):
    chapter: str | None = None     # chapter folder name; None = every raw chapter
    now: bool = False              # skip the idle wait


@router.post("/api/series/{series_id}/translate", dependencies=[Depends(require_token)])
def translate_series(series_id: int, body: TranslateIn) -> dict[str, Any]:
    if not mt.ocr_available():
        raise HTTPException(status_code=503, detail="OCR isn't installed (pip install rapidocr).")
    data = api._db.get_series_by_id(series_id)
    if not data:
        raise HTTPException(status_code=404, detail="Series not found")
    if body.chapter is None:
        return {"queued": enqueue_series(series_id, now=body.now)}
    ch = next((c for c in api._chapters_payload(str(data["folder_path"])) if c["name"] == body.chapter), None)
    if not ch:
        raise HTTPException(status_code=404, detail="Chapter not found")
    lang = (ch.get("language") or "").lower()
    if lang not in mt.RAW_LANGS:
        raise HTTPException(status_code=400, detail="Only raw Japanese/Korean/Chinese chapters can be translated.")
    if body.now:   # redo on demand: drop the old file so the new one replaces it
        try:
            (Path(ch["path"]) / mt.TRANSLATION_FILE).unlink(missing_ok=True)
        except OSError:
            pass
    queued = _enqueue(series_id, str(data.get("title") or ""), ch["name"], ch["path"], lang, body.now)
    return {"queued": int(queued)}


@router.get("/api/series/{series_id}/translation", dependencies=[Depends(require_token_read)])
def get_translation(series_id: int, chapter: str) -> dict[str, Any]:
    data = api._db.get_series_by_id(series_id)
    if not data:
        raise HTTPException(status_code=404, detail="Series not found")
    ch = next((c for c in api._chapters_payload(str(data["folder_path"])) if c["name"] == chapter), None)
    f = Path(ch["path"]) / mt.TRANSLATION_FILE if ch else None
    if not f or not f.is_file():
        raise HTTPException(status_code=404, detail="No translation for this chapter")
    return _json.loads(f.read_text(encoding="utf-8"))


@router.get("/api/translate/status", dependencies=[Depends(require_token_read)])
def translate_status() -> dict[str, Any]:
    with _LOCK:
        jobs = [{k: j.get(k) for k in ("id", "series_id", "title", "chapter", "status", "message", "error", "now")}
                for j in _Q]
    return {"available": mt.ocr_available(), "idle_seconds": int(mt.idle_seconds()),
            "jobs": jobs, "pending": sum(j["status"] in ("queued", "running", "waiting") for j in jobs)}


@router.get("/api/translate/models", dependencies=[Depends(require_token_read)])
def translate_models() -> dict[str, Any]:
    """Installed Ollama chat models, for the Settings picker."""
    try:
        with urllib.request.urlopen(f"{mt.OLLAMA}/api/tags", timeout=5) as r:
            names = [m["name"] for m in _json.loads(r.read()).get("models", [])]
    except Exception:
        names = []
    return {"models": [n for n in names if "embed" not in n.lower()], "default": mt.DEFAULT_MODEL}


@router.delete("/api/translate/queue", dependencies=[Depends(require_token)])
def clear_queue() -> dict[str, Any]:
    """Cancel everything still waiting (the chapter in progress finishes)."""
    with _LOCK:
        before = len(_Q)
        _Q[:] = [j for j in _Q if j["status"] not in ("queued", "waiting")]
        removed = before - len(_Q)
    _save()
    return {"removed": removed}
