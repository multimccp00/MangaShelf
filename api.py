"""FastAPI backend for the MangaShelf web app.

Uses the core library modules (database.py, scanner.py, sidecar.py) to read the
SQLite DB and the on-disk image library. Progress saves go through
Database.update_last_read_progress.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Make the project root importable so we can use the core library modules,
# and the web dir so the `sources` import-adapter package resolves.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_WEB_DIR = Path(__file__).resolve().parent
for _p in (str(_PROJECT_ROOT), str(_WEB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import sqlite3  # noqa: E402
import threading  # noqa: E402
import queue  # noqa: E402
import logging  # noqa: E402

_log = logging.getLogger("mangashelf")

from database import Database, DB_PATH  # noqa: E402
from scanner import (  # noqa: E402
    IMAGE_EXTENSIONS,
    get_first_image_path,
    get_series_chapters,
)
import pdf_support  # noqa: E402

from sources.registry import (  # noqa: E402
    find_source, list_sources, find_by_name, source_info,
    install_extension, remove_extension, set_extension_enabled, list_extensions,
    search_all, any_searchable,
)
from sources.downloader import download_series  # noqa: E402


# Build the connection ourselves with check_same_thread=False, then run the same
# setup the base __init__ does. database.py opens with sqlite3's default
# check_same_thread=True — which trips when FastAPI runs sync endpoints across its
# threadpool. Reopening with the flag off + a lock around access keeps the shared
# connection safe, and leaves database.py untouched.
def _make_web_db() -> Database:
    db = Database.__new__(Database)  # bypass __init__
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    db.conn.row_factory = sqlite3.Row
    db.conn.execute("PRAGMA foreign_keys = ON")
    db.conn.execute("PRAGMA journal_mode = WAL")
    db.conn.execute("PRAGMA synchronous = NORMAL")
    db.conn.execute("PRAGMA cache_size = -8000")
    db.conn.execute("PRAGMA temp_store = MEMORY")
    db._create_tables()
    db._migrate_schema()
    db._create_indexes()
    db._seed_default_genres()
    return db


_db_lock = threading.RLock()


class _LockedDB:
    """Proxy that serializes every method call on the shared Database through one
    lock, so concurrent FastAPI threadpool requests can't corrupt the connection."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def __getattr__(self, name: str):
        attr = getattr(self._db, name)
        if not callable(attr):
            return attr

        def _locked(*args, **kwargs):
            with _db_lock:
                return attr(*args, **kwargs)

        return _locked


_db = _LockedDB(_make_web_db())


# --------------------------------------------------------------------- helpers --

def _library_roots() -> list[Path]:
    """Resolved paths of every configured library. Used to sandbox image serving."""
    roots: list[Path] = []
    for lib in _db.get_libraries():
        try:
            roots.append(Path(str(lib["path"])).resolve())
        except OSError:
            continue
    return roots


def _is_inside_library(path: Path) -> bool:
    """True only if `path` lives under a known library root. Guards the page/cover
    endpoints so a crafted ?path= can't read arbitrary files off disk."""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in _library_roots():
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


# Characters Windows forbids in file/folder names, plus control chars.
_INVALID_NAME_RE = __import__("re").compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_name(name: str) -> str:
    """Make `name` safe to use as a single path component (no separators / reserved
    chars). Trailing dots/spaces are stripped (Windows rejects them)."""
    cleaned = _INVALID_NAME_RE.sub("", str(name or "")).strip().rstrip(". ")
    return cleaned


# Walking a big series' folders is slow — a 199-chapter series took ~8s cold
# (stat every image, open every PDF for its page count). The detail screen AND
# the reader both need this payload, and re-opening a series re-walked it every
# time. Cache the built payload per folder; invalidated on the same events that
# mutate a series on disk (rename/split/rescan/delete), which already clear the
# page-count + cover caches. Keyed by folder_path → payload (deep-copied out so
# callers can't mutate the cached object).
_CHAPTERS_CACHE: dict[str, list[dict[str, Any]]] = {}


def _invalidate_chapters_cache(series_path: str | None = None) -> None:
    """Drop one folder's cached chapters, or all of them when path is None."""
    with _CACHE_LOCK:
        if series_path is None:
            _CHAPTERS_CACHE.clear()
        else:
            _CHAPTERS_CACHE.pop(str(series_path), None)


def _chapters_payload(series_path: str) -> list[dict[str, Any]]:
    """Real chapters from disk with page counts and per-page image paths.
    Cached per folder (see _CHAPTERS_CACHE) — the walk is expensive for large
    series and both the detail screen and reader hit this on every open."""
    key = str(series_path)
    with _CACHE_LOCK:
        cached = _CHAPTERS_CACHE.get(key)
    if cached is not None:
        return [dict(c) for c in cached]   # shallow copy so callers can't mutate the cache

    chapters = get_series_chapters(series_path)
    out: list[dict[str, Any]] = []
    for idx, ch in enumerate(chapters):
        out.append(
            {
                "index": idx,
                "name": ch.name,
                "path": ch.path,
                "page_count": len(ch.images),
                "pages": ch.images,  # absolute paths; client requests them via /api/page
            }
        )
    with _CACHE_LOCK:
        _CHAPTERS_CACHE[key] = [dict(c) for c in out]
    return out


def _series_total_pages(chapters: list[dict[str, Any]]) -> int:
    return sum(int(c["page_count"]) for c in chapters)


def _chapter_page_starts(chapters: list[dict[str, Any]]) -> list[int]:
    """1-based global page index at which each chapter begins, across the
    flattened series. e.g. chapters of [10, 8, 12] pages -> [1, 11, 19]."""
    starts: list[int] = []
    running = 1
    for c in chapters:
        starts.append(running)
        running += int(c["page_count"])
    return starts


def _chapters_read(read_page: int, starts: list[int]) -> int:
    """How many chapters are fully completed given the current global read page.
    A chapter counts as read once the user has reached the FIRST page of the next
    chapter (i.e. read_page >= next chapter's start). The last chapter counts as
    read only when read_page reaches the total page count (handled by caller)."""
    if not starts or read_page <= 0:
        return 0
    n = 0
    for i in range(len(starts)):
        nxt = starts[i + 1] if i + 1 < len(starts) else None
        if nxt is not None and read_page >= nxt:
            n += 1
        elif nxt is None:
            # last chapter — caller passes total; counted as read when reached
            pass
    return n


# ------------------------------------------------------------- page-count cache --
# Total page count per series_id. Scanning every series on each /api/series call
# would be slow (thousands of folders), so we cache counts: populated whenever a
# series is opened (detail/reader scans it anyway) and persisted to disk so counts
# survive restarts. The library list merges these in.
import json as _json  # noqa: E402

# One lock guards BOTH module caches (_PAGE_COUNTS and _COVER_PATH_CACHE) and the
# page-count file write. FastAPI runs sync endpoints across a threadpool and there
# are background warmup/prewarm threads, so unguarded dict mutation could drop
# writes or raise "dict changed size during iteration" inside json.dumps.
import os as _os  # noqa: E402

_CACHE_LOCK = threading.RLock()

# Bounds concurrent expensive image work (PDF page renders + Pillow thumbnailing).
# Each decode can hold a multi-MB page in memory; a fast reader or several clients
# hitting the grid at once could otherwise fan out unbounded and spike memory. This
# semaphore makes bursts queue instead of all decoding simultaneously. Only wraps
# the actual generation on a cache MISS — cache hits (a FileResponse of an existing
# JPEG) don't touch it, so warm loads stay fully parallel.
_RENDER_SEM = threading.BoundedSemaphore(4)

_PAGECOUNT_PATH = Path.home() / ".mangashelf" / "web_pagecounts.json"
_PAGE_COUNTS: dict[str, int] = {}
try:
    if _PAGECOUNT_PATH.exists():
        _PAGE_COUNTS = _json.loads(_PAGECOUNT_PATH.read_text(encoding="utf-8"))
except Exception:
    _PAGE_COUNTS = {}

# Per-series chapter metadata for the library list: chapter count + the 1-based
# global page index where each chapter starts (so we can show "chapters read /
# total" without the client loading every chapter). Same warm-up path as page
# counts; persisted alongside them. Keyed by str(series_id):
#   { "n": <chapter count>, "starts": [1, 11, 19, ...] }
_CHAPTERMETA_PATH = Path.home() / ".mangashelf" / "web_chaptermeta.json"
_CHAPTER_META: dict[str, dict[str, Any]] = {}
try:
    if _CHAPTERMETA_PATH.exists():
        _CHAPTER_META = _json.loads(_CHAPTERMETA_PATH.read_text(encoding="utf-8"))
except Exception:
    _CHAPTER_META = {}


def _save_chapter_meta() -> None:
    with _CACHE_LOCK:
        snapshot = dict(_CHAPTER_META)
    try:
        tmp = _CHAPTERMETA_PATH.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps(snapshot), encoding="utf-8")
        _os.replace(tmp, _CHAPTERMETA_PATH)
    except Exception:
        pass


def _remember_chapter_meta(series_id: int, chapters: list[dict[str, Any]]) -> None:
    meta = {"n": len(chapters), "starts": _chapter_page_starts(chapters)}
    with _CACHE_LOCK:
        if _CHAPTER_META.get(str(series_id)) == meta:
            return
        _CHAPTER_META[str(series_id)] = meta
    _save_chapter_meta()


def _save_page_counts() -> None:
    """Atomically persist the counts. Snapshot under the lock, then write to a temp
    file and os.replace() so a crash mid-write can't truncate the real file."""
    with _CACHE_LOCK:
        snapshot = dict(_PAGE_COUNTS)
    try:
        tmp = _PAGECOUNT_PATH.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps(snapshot), encoding="utf-8")
        _os.replace(tmp, _PAGECOUNT_PATH)
    except Exception:
        pass


def _remember_page_count(series_id: int, total: int) -> None:
    with _CACHE_LOCK:
        if _PAGE_COUNTS.get(str(series_id)) == total:
            return
        _PAGE_COUNTS[str(series_id)] = total
    _save_page_counts()


# Background warm-up: the library list queues uncached series here, and a single
# worker thread scans their folders for page counts without blocking responses.
# A set guards against re-queueing the same series while it's still pending.
_WARMUP_QUEUE: "queue.Queue[tuple[int, str]]" = None  # type: ignore[assignment]
_WARMUP_PENDING: set[int] = set()
_WARMUP_LOCK = threading.Lock()


def _queue_page_count_warmup(items: list[tuple[int, str]]) -> None:
    global _WARMUP_QUEUE
    with _WARMUP_LOCK:
        if _WARMUP_QUEUE is None:
            _WARMUP_QUEUE = queue.Queue()
            threading.Thread(
                target=_warmup_worker, name="pagecount-warmup", daemon=True
            ).start()
        for sid, folder in items:
            if sid in _WARMUP_PENDING or str(sid) in _PAGE_COUNTS:
                continue
            _WARMUP_PENDING.add(sid)
            _WARMUP_QUEUE.put((sid, folder))


def _warmup_worker() -> None:
    while True:
        sid, folder = _WARMUP_QUEUE.get()
        try:
            chapters = _chapters_payload(folder)
            _remember_page_count(sid, _series_total_pages(chapters))
            _remember_chapter_meta(sid, chapters)
        except Exception:
            # Unreadable/missing folder — leave uncached, retry next load. Log so a
            # SYSTEMATICALLY failing folder (perpetual "Pg 0/0") is discoverable.
            _log.warning("page-count warmup failed for series %s (%s)", sid, folder, exc_info=True)
        finally:
            with _WARMUP_LOCK:
                _WARMUP_PENDING.discard(sid)
            _WARMUP_QUEUE.task_done()


# ----------------------------------------------------------------- cover cache --

# Resolved cover source path per series_id (avoids re-walking the folder).
_COVER_PATH_CACHE: dict[int, str] = {}

# On-disk thumbnail cache, kept out of the user's library.
_THUMB_DIR = Path.home() / ".mangashelf" / "web_thumbs"
_THUMB_DIR.mkdir(parents=True, exist_ok=True)
_THUMB_WIDTH = 360  # plenty for a library grid cell

# User-uploaded custom covers, one per series, stored in the app data folder
# (never inside the user's library). Takes precedence over the auto-resolved
# cover and survives rescans.
_COVER_OVERRIDE_DIR = Path.home() / ".mangashelf" / "covers"
_COVER_OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)


def _override_cover_path(series_id: int) -> Path:
    return _COVER_OVERRIDE_DIR / f"{series_id}.jpg"


def _cover_version(series_id: int) -> int:
    """A version token for a series' cover, baked into the cover URL by the client.

    Covers are cached in-browser for a week (good — they rarely change), but that
    meant a freshly-uploaded/removed custom cover kept showing the stale cached
    image because the URL was identical. Returning the override file's mtime makes
    the URL change the instant the cover changes, so the cache stays effective AND
    correct. 0 == no custom cover (the auto cover, which is path+mtime-keyed)."""
    try:
        return int(_override_cover_path(series_id).stat().st_mtime)
    except OSError:
        return 0


def _save_source_cover(series_id: int, cover_url: str, headers: dict) -> bool:
    """Download an imported series' source cover and store it as its custom cover
    (so the grid/detail show the real cover, not an auto-picked first page)."""
    if not cover_url or not _PIL_OK:
        return False
    import io
    import urllib.request
    _MAX_COVER_BYTES = 40 * 1024 * 1024   # a cover over 40 MB is a wrong/hostile URL
    try:
        req = urllib.request.Request(cover_url, headers={"User-Agent": "MangaShelf/1.0", **(headers or {})})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read(_MAX_COVER_BYTES + 1)
        if len(raw) > _MAX_COVER_BYTES:
            raise ValueError("cover exceeds size cap")
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        im.save(_override_cover_path(series_id), "JPEG", quality=88)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[cover] could not save source cover for {series_id}: {exc}")
        return False

try:
    from PIL import Image  # Pillow is already a project dependency
    _PIL_OK = True
except Exception:  # pragma: no cover
    _PIL_OK = False


_PSD_EXTS = {".psd", ".psb"}


def _pil_open_flat(src: Path):
    """Open any image with Pillow and return a flat RGB Image.

    PSD/PSB files need special handling: Pillow loads them in RGBa/RGBA mode
    (merged composite) — we flatten onto white before converting to RGB so the
    result looks correct instead of transparent-black.
    """
    from PIL import Image
    im = Image.open(src)
    # Force load so the pixel data is decoded (needed before mode checks on PSD).
    im.load()
    if im.mode in ("RGBA", "RGBa", "LA", "La", "PA"):
        bg = Image.new("RGB", im.size, (255, 255, 255))
        mask = im.split()[-1] if im.mode not in ("LA", "La") else im.split()[1]
        bg.paste(im.convert("RGB"), mask=mask)
        return bg
    return im.convert("RGB")


def _get_or_make_thumbnail(src: Path) -> Path | None:
    """Return a cached downscaled JPEG for `src`, creating it if needed.

    Cache key is source path + mtime + size, so an edited/replaced page busts it.
    Returns None on any failure so the caller can fall back to the original file.
    """
    if not _PIL_OK:
        return None
    try:
        st = src.stat()
    except OSError:
        return None
    import hashlib

    key = f"{src}|{int(st.st_mtime)}|{st.st_size}|{_THUMB_WIDTH}"
    name = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest() + ".jpg"
    thumb_path = _THUMB_DIR / name
    if thumb_path.exists():
        return thumb_path
    with _RENDER_SEM:   # bound concurrent decodes (see _RENDER_SEM)
        # Re-check under the semaphore: a concurrent caller may have just made it.
        if thumb_path.exists():
            return thumb_path
        try:
            im = _pil_open_flat(src)
            w, h = im.size
            if w > _THUMB_WIDTH:
                im = im.resize((_THUMB_WIDTH, int(h * _THUMB_WIDTH / w)), Image.LANCZOS)
            im.save(thumb_path, "JPEG", quality=82, optimize=True)
            return thumb_path
        except Exception:
            # Corrupt image, unsupported mode, etc. — fall back to serving the original.
            return None


def _render_pdf_page_cached(pdf_path: str, page_no: int) -> Path | None:
    """Render a PDF page to a cached JPEG on disk and return its path. Keyed by
    the PDF's path+mtime+size+page so an edited file refreshes. None on failure."""
    if not pdf_support.pdf_available():
        return None
    try:
        st = Path(pdf_path).stat()
    except OSError:
        return None
    import hashlib
    key = f"{pdf_path}|{int(st.st_mtime)}|{st.st_size}|p{page_no}"
    name = "pdf_" + hashlib.sha1(key.encode("utf-8", "replace")).hexdigest() + ".jpg"
    out = _THUMB_DIR / name
    if out.exists():
        return out
    with _RENDER_SEM:   # bound concurrent PDF renders (see _RENDER_SEM)
        if out.exists():
            return out
        try:
            data = pdf_support.render_page_to_jpeg_bytes(pdf_path, page_no)
            out.write_bytes(data)
            return out
        except Exception:
            return None


# DPI for THUMBNAIL renders of PDF pages — far lower than reading DPI (150). At
# ~36 DPI a page is ~360px wide: a ~25 KB JPEG instead of the multi-MB full page,
# and renders in a fraction of the time. Used for the chapter-card grid.
_THUMB_PDF_DPI = 36


def _render_pdf_thumb_cached(pdf_path: str, page_no: int) -> Path | None:
    """Like _render_pdf_page_cached but at thumbnail resolution + size — the
    chapter grid was serving full-res reading pages (multi-MB, ~2s each) as tiny
    thumbnails, which was the real source of the detail-screen lag."""
    if not pdf_support.pdf_available():
        return None
    try:
        st = Path(pdf_path).stat()
    except OSError:
        return None
    import hashlib
    # v2 in the key forces regeneration after the crop-to-cover-ratio change.
    key = f"{pdf_path}|{int(st.st_mtime)}|{st.st_size}|p{page_no}|thumb{_THUMB_WIDTH}|v2"
    name = "pdfthumb_" + hashlib.sha1(key.encode("utf-8", "replace")).hexdigest() + ".jpg"
    out = _THUMB_DIR / name
    if out.exists():
        return out
    with _RENDER_SEM:   # bound concurrent PDF renders (see _RENDER_SEM)
        if out.exists():
            return out
        try:
            data = pdf_support.render_page_to_jpeg_bytes(pdf_path, page_no, dpi=_THUMB_PDF_DPI)
            # Downscale to _THUMB_WIDTH and crop the TOP to a cover aspect ratio. The
            # chapter card only shows the top of the page anyway, and webtoon pages are
            # extremely tall (e.g. 360×2500 ≈ 150 KB) — cropping cuts that to ~20 KB.
            if _PIL_OK:
                try:
                    import io
                    from PIL import Image
                    im = Image.open(io.BytesIO(data)).convert("RGB")
                    if im.width > _THUMB_WIDTH:
                        im = im.resize((_THUMB_WIDTH, int(im.height * _THUMB_WIDTH / im.width)), Image.LANCZOS)
                    target_h = int(im.width * 7 / 5)        # 5:7 cover ratio
                    if im.height > target_h:
                        im = im.crop((0, 0, im.width, target_h))
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=78, optimize=True)
                    data = buf.getvalue()
                except Exception:
                    pass
            out.write_bytes(data)
            return out
        except Exception:
            return None


def _first_valid_cover(folder: str) -> tuple[str | None, Path | None]:
    """Resolve a cover that actually decodes.

    The normal cover (`get_first_image_path`) just takes the first file by name —
    but a folder's first page is sometimes corrupt (bad/garbage bytes despite a
    .jpg extension), which renders blank in the browser. Here we walk images in
    natural order and return the first one whose thumbnail can be generated,
    skipping undecodable files. Returns (cover_source_path, thumbnail_path).
    """
    primary = get_first_image_path(folder)
    if not primary:
        return None, None
    # PDF cover token ('<pdf>#N') — render that page; its rendered JPEG IS the thumb.
    base, pdf_page = pdf_support.split_page_token(primary)
    if pdf_page is not None:
        rendered = _render_pdf_page_cached(base, pdf_page)
        return primary, rendered
    # Fast path: the primary cover decodes fine.
    thumb = _get_or_make_thumbnail(Path(primary))
    if thumb is not None:
        return primary, thumb

    # Primary failed to decode — scan the folder for the next valid image.
    fpath = Path(folder)
    try:
        from scanner import is_image, natural_key
        candidates = sorted(
            (e for e in fpath.iterdir() if e.is_file() and is_image(e)),
            key=lambda e: natural_key(e.name),
        )
    except Exception:
        candidates = []
    for cand in candidates:
        if str(cand) == str(primary):
            continue  # already known-bad
        t = _get_or_make_thumbnail(cand)
        if t is not None:
            return str(cand), t
    # Nothing decoded — hand back the primary so the caller can 404/placeholder.
    return primary, None


# ---------------------------------------------------- thumbnail pre-warm at startup --
# Generate thumbnails for every series in the background using a thread pool so
# the first grid load hits the disk cache instead of blocking on Pillow.

def _prewarm_thumbnails() -> None:
    from concurrent.futures import ThreadPoolExecutor
    rows = _db.get_series_list()
    def _warm_one(row: dict) -> None:
        sid = int(row.get("id", 0))
        folder = str(row.get("folder_path") or "")
        if not folder:
            return
        # Re-use the same cover-path cache so requests never re-walk.
        with _CACHE_LOCK:
            cover = _COVER_PATH_CACHE.get(sid)
        if cover is None:
            cover = get_first_image_path(folder)
            with _CACHE_LOCK:
                _COVER_PATH_CACHE[sid] = cover or ""
        if cover:
            _get_or_make_thumbnail(Path(cover))  # writes to disk if not cached

    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="thumb-warm") as ex:
        ex.map(_warm_one, rows)

threading.Thread(target=_prewarm_thumbnails, name="thumb-prewarm", daemon=True).start()


# ------------------------------------------------------------------ auth --
# A per-install secret token gates the whole JSON/image API. Mutating endpoints
# (POST/DELETE) require it in the X-Mangashelf-Token header. Read endpoints
# (GET) require it too — via that header OR a ?token= query param, because
# <img src> tags (covers/pages) can't send custom headers. This means the app is
# safe to expose beyond localhost: without the token nothing can be read or
# written. The token is generated once, persisted, and injected into index.html
# by server.py so the app's own fetches/images can attach it. The HTML shell,
# vendored JS, and static assets stay open (they carry no library data and the
# page can't supply a token before it has loaded).
import secrets as _secrets  # noqa: E402

_TOKEN_PATH = Path.home() / ".mangashelf" / "web_token.txt"


def _load_or_create_token() -> str:
    try:
        if _TOKEN_PATH.exists():
            tok = _TOKEN_PATH.read_text(encoding="utf-8").strip()
            if tok:
                return tok
    except OSError:
        pass
    tok = _secrets.token_urlsafe(32)
    try:
        _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_PATH.write_text(tok, encoding="utf-8")
    except OSError:
        pass
    return tok


WEB_TOKEN = _load_or_create_token()


def _token_ok(tok: str | None) -> bool:
    return bool(tok) and _secrets.compare_digest(tok, WEB_TOKEN)


# ------------------------------------------------------------- app password --
# Defense-in-depth on top of the per-install token: when a password is set, the
# page is served WITHOUT the token (see server.py), so merely reaching the URL —
# e.g. another device on the same tailnet — isn't enough. The user must POST the
# password to /api/login to receive the token, which then unlocks the API exactly
# as before. The password is stored only as a salted hash (PBKDF2), never plain.
import hashlib as _hashlib  # noqa: E402

_PASSWORD_PATH = Path.home() / ".mangashelf" / "web_password.json"
_PBKDF2_ROUNDS = 200_000


def _hash_password(password: str, salt: bytes) -> str:
    dk = _hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return dk.hex()


def _load_password_record() -> dict[str, str] | None:
    """The stored {salt, hash} record, or None if no password is configured."""
    try:
        if _PASSWORD_PATH.exists():
            rec = _json.loads(_PASSWORD_PATH.read_text(encoding="utf-8"))
            if isinstance(rec, dict) and rec.get("salt") and rec.get("hash"):
                return rec
    except Exception:
        pass
    return None


def _password_is_set() -> bool:
    return _load_password_record() is not None


def _password_matches(password: str) -> bool:
    rec = _load_password_record()
    if not rec:
        return False
    try:
        salt = bytes.fromhex(rec["salt"])
    except ValueError:
        return False
    candidate = _hash_password(password or "", salt)
    return _secrets.compare_digest(candidate, rec["hash"])


def _set_password(password: str) -> None:
    """Set (or clear, if empty) the app password. Written atomically."""
    if not password:
        # Clearing the password reverts to token-auto-injection (open on tailnet).
        try:
            _PASSWORD_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        return
    salt = _secrets.token_bytes(16)
    rec = {"salt": salt.hex(), "hash": _hash_password(password, salt)}
    _PASSWORD_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PASSWORD_PATH.with_suffix(".json.tmp")
    tmp.write_text(_json.dumps(rec), encoding="utf-8")
    _os.replace(tmp, _PASSWORD_PATH)


def require_token(x_mangashelf_token: str | None = Header(default=None)) -> None:
    """Dependency for MUTATING routes — token must arrive in the header (a query
    param would leak into logs/referrers and isn't needed for fetch() calls)."""
    if not _token_ok(x_mangashelf_token):
        raise HTTPException(status_code=401, detail="Missing or invalid auth token")


def require_token_read(
    x_mangashelf_token: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> None:
    """Dependency for READ routes — accepts the token in the header (fetch) OR a
    ?token= query param (so <img src> cover/page URLs, which can't set headers,
    still authenticate)."""
    if not (_token_ok(x_mangashelf_token) or _token_ok(token)):
        raise HTTPException(status_code=401, detail="Missing or invalid auth token")


# ------------------------------------------------------------------- settings --
# App preferences (startup mode, confirm-private, show-switcher, …) live here on
# the server so they survive browser-storage clearing and stay in sync across the
# user's devices. Stored as one small JSON blob, written atomically.
_SETTINGS_PATH = Path.home() / ".mangashelf" / "web_settings.json"
_SETTINGS_LOCK = threading.Lock()
# Defaults applied when a key isn't present yet.
_SETTINGS_DEFAULTS: dict[str, Any] = {
    "startMode": "default",      # "default" | "last"
    "confirmPrivate": True,
    "showSwitcher": True,
    "hidePrivate": False,        # hide private libraries from the UI (not delete)
    "lastLibraryId": None,
}


def _load_settings() -> dict[str, Any]:
    data: dict[str, Any] = {}
    try:
        if _SETTINGS_PATH.exists():
            data = _json.loads(_SETTINGS_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        data = {}
    merged = dict(_SETTINGS_DEFAULTS)
    if isinstance(data, dict):
        merged.update(data)
    return merged


def _save_settings(patch: dict[str, Any]) -> dict[str, Any]:
    with _SETTINGS_LOCK:
        current = _load_settings()
        # Only accept known keys so the client can't write arbitrary data.
        for k, v in (patch or {}).items():
            if k in _SETTINGS_DEFAULTS:
                current[k] = v
        try:
            tmp = _SETTINGS_PATH.with_suffix(".json.tmp")
            _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(_json.dumps(current), encoding="utf-8")
            _os.replace(tmp, _SETTINGS_PATH)
        except Exception:
            pass
        return current


# ------------------------------------------------------------------------- app --

app = FastAPI(title="MangaShelf Web", version="0.1.0")


@app.get("/api/series", dependencies=[Depends(require_token_read)])
def list_series(
    search: str = "",
    filter_mode: str = "All",
    status_filter: str | None = None,
    sort_mode: str = "Title A-Z",
    tag_filter: str | None = None,
    series_filter: str | None = None,
    genre_filter: str | None = None,
    parody_filter: str | None = None,
    library: int | None = None,
) -> list[dict[str, Any]]:
    """Library listing, scoped to the active library (?library=ID) so only that
    library's content is ever returned. Merges in cached total page counts."""
    rows = _db.get_series_list(
        search=search,
        filter_mode=filter_mode,
        status_filter=status_filter,
        sort_mode=sort_mode,
        tag_filter=tag_filter,
        series_filter=series_filter,
        genre_filter=genre_filter,
        parody_filter=parody_filter,
        library_id=library,
    )
    missing: list[tuple[int, str]] = []
    with _CACHE_LOCK:
        counts_snapshot = dict(_PAGE_COUNTS)
        chmeta_snapshot = dict(_CHAPTER_META)
    for r in rows:
        sid = r.get("id")
        r["cover_v"] = _cover_version(int(sid)) if sid is not None else 0
        # Chapter info for the card: count + chapters-read derived from last_page.
        # Cards show "chapters read / total" for multi-chapter series, and fall
        # back to pages for single-chapter ones (the client decides via these).
        meta = chmeta_snapshot.get(str(sid))
        if meta:
            n = int(meta.get("n", 0))
            starts = meta.get("starts") or []
            r["chapter_count"] = n
            read_page = int(r.get("last_page") or 0)
            total_cached = counts_snapshot.get(str(sid))
            # Last chapter counts as read only when the final page is reached.
            done = _chapters_read(read_page, starts)
            if n and total_cached and read_page >= total_cached:
                done = n
            r["chapters_read"] = done
        cached = counts_snapshot.get(str(sid))
        if cached is not None:
            r["total_pages"] = cached
        else:
            # Never opened, so the count was never scanned. Don't scan inline —
            # scanning thousands of folders on the request thread times the call
            # out. Queue it for a background warm-up; the next list load will pick
            # up the cached value. (Otherwise unread series show "Pg. 0 / 0".)
            folder = r.get("folder_path")
            if folder:
                missing.append((int(sid), str(folder)))
    if missing:
        _queue_page_count_warmup(missing)
    return rows


@app.get("/api/series/{series_id}", dependencies=[Depends(require_token_read)])
def get_series(series_id: int) -> dict[str, Any]:
    data = _db.get_series_by_id(series_id)
    if not data:
        raise HTTPException(status_code=404, detail="Series not found")
    chapters = _chapters_payload(str(data["folder_path"]))
    data["chapters"] = chapters
    total = _series_total_pages(chapters)
    data["total_pages"] = total
    data["chapter_count"] = len(chapters)
    data["cover_v"] = _cover_version(series_id)
    # Origin info (where it was imported from), for the detail page + re-sync.
    data["origin"] = source_info(str(data.get("external_id") or ""))
    _remember_page_count(series_id, total)  # so the library list can show read/total
    _remember_chapter_meta(series_id, chapters)
    return data


@app.get("/api/series/{series_id}/chapters", dependencies=[Depends(require_token_read)])
def get_chapters(series_id: int) -> list[dict[str, Any]]:
    data = _db.get_series_by_id(series_id)
    if not data:
        raise HTTPException(status_code=404, detail="Series not found")
    chapters = _chapters_payload(str(data["folder_path"]))
    _remember_page_count(series_id, _series_total_pages(chapters))
    _remember_chapter_meta(series_id, chapters)
    return chapters


@app.post("/api/series/{series_id}/cover", dependencies=[Depends(require_token)])
async def upload_cover(series_id: int, file: UploadFile = File(...)):
    """Set a custom cover for a series from an uploaded image. The image is
    re-encoded to JPEG (validates it's a real image, strips metadata) and stored
    in the app data folder, keyed by series id. Overrides the auto cover."""
    data = _db.get_series_by_id(series_id)
    if not data:
        raise HTTPException(status_code=404, detail="Series not found")
    if not _PIL_OK:
        raise HTTPException(status_code=415, detail="Pillow not available")
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty upload")
    if len(raw) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image too large (max 25 MB)")
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        im.load()
        if im.mode in ("RGBA", "RGBa", "LA", "La", "PA"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            mask = im.split()[-1] if im.mode not in ("LA", "La") else im.split()[1]
            bg.paste(im.convert("RGB"), mask=mask)
            im = bg
        else:
            im = im.convert("RGB")
        # Downscale very large uploads so the stored cover stays reasonable.
        if im.width > 1200:
            im = im.resize((1200, int(im.height * 1200 / im.width)), Image.LANCZOS)
        out = _override_cover_path(series_id)
        tmp = out.with_suffix(".jpg.tmp")
        im.save(tmp, "JPEG", quality=88, optimize=True)
        _os.replace(tmp, out)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Not a valid image: {exc}")
    # Drop any cached resolution so the override is picked up immediately.
    with _CACHE_LOCK:
        _COVER_PATH_CACHE.pop(series_id, None)
    return {"ok": True}


@app.delete("/api/series/{series_id}/cover", dependencies=[Depends(require_token)])
def remove_cover(series_id: int) -> dict[str, Any]:
    """Remove a custom cover override; reverts to the auto-resolved cover."""
    try:
        _override_cover_path(series_id).unlink(missing_ok=True)
    except OSError:
        pass
    with _CACHE_LOCK:
        _COVER_PATH_CACHE.pop(series_id, None)
    return {"ok": True}


@app.get("/api/series/{series_id}/cover", dependencies=[Depends(require_token_read)])
def get_cover(series_id: int):
    data = _db.get_series_by_id(series_id)
    if not data:
        raise HTTPException(status_code=404, detail="Series not found")

    # A user-uploaded override always wins over the auto-resolved cover.
    override = _override_cover_path(series_id)
    if override.exists():
        return FileResponse(str(override), headers={"Cache-Control": "no-cache"})

    # Resolve a cover that actually DECODES. A folder's first page is sometimes
    # corrupt (garbage bytes under a .jpg name) or an unreadable format — that would
    # render blank in the browser. _first_valid_cover skips bad files to the next
    # decodable image. We cache the resolved source path per series_id so repeat
    # requests skip the (slow on network drives) folder walk.
    with _CACHE_LOCK:
        cover = _COVER_PATH_CACHE.get(series_id)
    thumb: Path | None = None
    if cover:
        cbase, cpdf = pdf_support.split_page_token(cover)
        thumb = _render_pdf_page_cached(cbase, cpdf) if cpdf is not None else _get_or_make_thumbnail(Path(cover))
    if not cover or thumb is None:
        cover, thumb = _first_valid_cover(str(data["folder_path"]))
        with _CACHE_LOCK:
            _COVER_PATH_CACHE[series_id] = cover or ""

    # Containment check uses the real file (strip any '#page' suffix for PDFs).
    cover_base = pdf_support.split_page_token(cover)[0] if cover else ""
    if not cover or not _is_inside_library(Path(cover_base)):
        raise HTTPException(status_code=404, detail="No cover available")

    # A small cached thumbnail renders instantly in the grid (full pages can be
    # multi-MB). Thumbnails are keyed by source path + mtime so they refresh if the
    # underlying image changes.
    if thumb:
        return FileResponse(
            str(thumb),
            headers={"Cache-Control": "public, max-age=604800"},  # cache 1 week in-browser
        )

    # No thumbnail could be made (and no other page decoded). Convert PSD/PSB on the
    # fly; otherwise the file is undecodable — 404 so the client shows a placeholder
    # instead of a blank card from a raw, unrenderable file.
    cover_path = Path(cover)
    if cover_path.suffix.lower() in _PSD_EXTS and _PIL_OK:
        try:
            import io
            im = _pil_open_flat(cover_path)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=88, optimize=True)
            buf.seek(0)
            from fastapi.responses import StreamingResponse
            return StreamingResponse(
                buf, media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=604800"},
            )
        except Exception:
            pass

    raise HTTPException(status_code=404, detail="Cover not decodable")


@app.get("/api/page", dependencies=[Depends(require_token_read)])
def get_page(
    path: str = Query(..., description="Page token: an image path, or '<pdf>#<page>'"),
    thumb: int = Query(0, description="1 = return a small thumbnail instead of the full page"),
):
    """Stream a single page image. Sandboxed to configured library roots.
    - PDF page tokens ('<file.pdf>#N') are rendered to JPEG on the fly (cached).
    - PSD/PSB files are converted to JPEG since browsers can't display them.
    - thumb=1 returns a small, fast thumbnail (for the chapter-card grid) instead
      of the multi-MB full reading page."""
    want_thumb = bool(thumb)
    # PDF page token? Render the requested page.
    base, pdf_page = pdf_support.split_page_token(path)
    if pdf_page is not None:
        bp = Path(base)
        if not _is_inside_library(bp):
            raise HTTPException(status_code=403, detail="Path outside library")
        if not bp.exists() or not bp.is_file():
            raise HTTPException(status_code=404, detail="PDF not found")
        if not pdf_support.pdf_available():
            raise HTTPException(status_code=415, detail="PyMuPDF not installed for PDF reading")
        rendered = (
            _render_pdf_thumb_cached(str(bp), pdf_page) if want_thumb
            else _render_pdf_page_cached(str(bp), pdf_page)
        )
        if rendered is None:
            raise HTTPException(status_code=500, detail="PDF page render failed")
        return FileResponse(str(rendered), headers={"Cache-Control": "public, max-age=604800"})

    p = Path(path)
    if p.suffix.lower() not in IMAGE_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Not an image")
    if not _is_inside_library(p):
        raise HTTPException(status_code=403, detail="Path outside library")
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="Page not found")

    # Thumbnail request for a plain image → serve the cached downscaled version.
    if want_thumb and p.suffix.lower() not in _PSD_EXTS:
        t = _get_or_make_thumbnail(p)
        if t is not None:
            return FileResponse(str(t), headers={"Cache-Control": "public, max-age=604800"})

    if p.suffix.lower() in _PSD_EXTS:
        # Browsers can't render PSD — convert to JPEG in memory and stream it.
        if not _PIL_OK:
            raise HTTPException(status_code=415, detail="Pillow not available for PSD conversion")
        try:
            import io
            from PIL import Image
            im = _pil_open_flat(p)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=92, optimize=True)
            buf.seek(0)
            from fastapi.responses import StreamingResponse
            return StreamingResponse(buf, media_type="image/jpeg")
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"PSD conversion failed: {exc}")

    # Set the media type explicitly — Windows' mimetypes often doesn't know .webp,
    # which would otherwise be served as application/octet-stream (some browsers
    # then refuse to render it in an <img>).
    _MEDIA = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".jpe": "image/jpeg",
        ".jfif": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
        ".gif": "image/gif", ".bmp": "image/bmp",
    }
    media = _MEDIA.get(p.suffix.lower())
    return FileResponse(str(p), media_type=media) if media else FileResponse(str(p))


@app.get("/api/facets", dependencies=[Depends(require_token_read)])
def get_facets(library: int | None = None) -> dict[str, Any]:
    """Distinct values for the library filter dropdowns and search panel, scoped
    to the active library so a public library never shows another's tags/genres."""
    items = _db.get_series_list(library_id=library)
    series_names = sorted(
        {
            str(x.get("series_name") or "").strip()
            for x in items
            if str(x.get("series_name") or "").strip()
        },
        key=str.lower,
    )
    authors = sorted(
        {str(x.get("author") or "").strip() for x in items if str(x.get("author") or "").strip()},
        key=str.lower,
    )
    languages = sorted(
        {str(x.get("language") or "").strip() for x in items if str(x.get("language") or "").strip()},
        key=str.lower,
    )

    # Usage counts: how many series carry each value. Drives the autocomplete
    # suggestions and the count shown on each pill (grouped-pill layout).
    tag_counts: dict[str, int] = {}
    genre_counts: dict[str, int] = {}
    author_counts: dict[str, int] = {}
    series_counts: dict[str, int] = {}
    language_counts: dict[str, int] = {}
    parody_counts: dict[str, int] = {}

    def _bump(d: dict[str, int], v: object) -> None:
        s = str(v or "").strip()
        if s:
            d[s] = d.get(s, 0) + 1

    for x in items:
        for t in (x.get("tags") or []):
            _bump(tag_counts, t)
        for g in (x.get("genres") or []):
            _bump(genre_counts, g)
        for p in (x.get("parodies") or []):
            _bump(parody_counts, p)
        _bump(author_counts, x.get("author"))
        _bump(series_counts, x.get("series_name"))
        _bump(language_counts, x.get("language"))

    # Tags/genres/parodies are derived from THIS library's series (not the global
    # lists) so the filter dropdowns stay scoped to the active library.
    scoped_genres = sorted(genre_counts.keys(), key=str.lower)
    scoped_tags = sorted(tag_counts.keys(), key=str.lower)
    scoped_parodies = sorted(parody_counts.keys(), key=str.lower)

    return {
        "genres": scoped_genres,
        "tags": scoped_tags,
        "parodies": scoped_parodies,
        "authors": authors,
        "series": series_names,
        "languages": languages,
        "statuses": ["Not Started", "Reading", "Completed", "On Hold", "Dropped", "Planned to Read"],
        "tag_counts": tag_counts,
        "genre_counts": genre_counts,
        "parody_counts": parody_counts,
        "author_counts": author_counts,
        "series_counts": series_counts,
        "language_counts": language_counts,
    }


class ProgressIn(BaseModel):
    chapter_name: str
    page: int


@app.post("/api/series/{series_id}/progress", dependencies=[Depends(require_token)])
def save_progress(series_id: int, body: ProgressIn) -> dict[str, Any]:
    """Persist reading progress back to the DB."""
    data = _db.get_series_by_id(series_id)
    if not data:
        raise HTTPException(status_code=404, detail="Series not found")
    _db.update_last_read_progress(series_id, body.chapter_name, body.page)
    return {"ok": True, "series_id": series_id, "chapter": body.chapter_name, "page": body.page}


class FavoriteIn(BaseModel):
    favorite: bool


@app.post("/api/series/{series_id}/favorite", dependencies=[Depends(require_token)])
def set_favorite(series_id: int, body: FavoriteIn) -> dict[str, Any]:
    data = _db.get_series_by_id(series_id)
    if not data:
        raise HTTPException(status_code=404, detail="Series not found")
    _db.set_favorite(series_id, body.favorite)
    return {"ok": True, "favorite": body.favorite}


class StatusIn(BaseModel):
    status: str


@app.post("/api/series/{series_id}/status", dependencies=[Depends(require_token)])
def set_status(series_id: int, body: StatusIn) -> dict[str, Any]:
    data = _db.get_series_by_id(series_id)
    if not data:
        raise HTTPException(status_code=404, detail="Series not found")
    _db.set_status(series_id, body.status)
    return {"ok": True, "status": body.status}


class MetadataIn(BaseModel):
    # All optional — only provided fields are changed; the rest keep their value.
    tags: list[str] | None = None
    genres: list[str] | None = None
    parodies: list[str] | None = None
    author: str | None = None
    series: str | None = None
    language: str | None = None
    notes: str | None = None
    rating: int | None = None


@app.post("/api/series/{series_id}/metadata", dependencies=[Depends(require_token)])
def update_metadata(series_id: int, body: MetadataIn) -> dict[str, Any]:
    """Update editable metadata (tags/genres/author/series/language/notes).

    Uses Database.update_series_metadata for DB rules (dedup, sorting, genre table
    upserts), then syncs the on-disk sidecar so changes survive a rescan."""
    data = _db.get_series_by_id(series_id)
    if not data:
        raise HTTPException(status_code=404, detail="Series not found")

    def pick(new, old):
        return old if new is None else new

    tags = pick(body.tags, data.get("tags") or [])
    genres = pick(body.genres, data.get("genres") or [])
    parodies = pick(body.parodies, data.get("parodies") or [])
    author = pick(body.author, data.get("author") or "")
    series_name = pick(body.series, data.get("series_name") or "")
    language = pick(body.language, data.get("language") or "")
    notes = pick(body.notes, data.get("notes") or "")
    rating = pick(body.rating, data.get("rating") or 0)

    _db.update_series_metadata(
        series_id=series_id,
        title=str(data.get("title") or "Untitled"),
        series_name=str(series_name or ""),
        author=str(author or ""),
        status=str(data.get("status") or "Not Started"),
        rating=int(rating or 0),
        favorite=bool(data.get("favorite")),
        notes=str(notes or ""),
        language=str(language or ""),
        genres=list(genres),
        tags=list(tags),
        parodies=list(parodies),
    )

    # Mirror into the sidecar from the FRESH DB row. write_sidecar always rewrites
    # its full owned key set, defaulting any omitted key from the EXISTING sidecar
    # — so if we passed only the just-edited fields, status/favorite/rating/
    # last_read would be frozen to stale sidecar values and a later rescan would
    # silently revert them. Sourcing every field from the post-update DB row keeps
    # the sidecar authoritative and prevents reversion.
    fresh = _db.get_series_by_id(series_id)
    try:
        from sidecar import read_sidecar, write_sidecar

        folder = str((fresh or data).get("folder_path") or "").strip()
        if folder:
            src = fresh or {}
            sc = read_sidecar(folder) or {}
            sc.update({
                "title": str(src.get("title") or ""),
                "series": str(src.get("series_name") or ""),
                "author": str(src.get("author") or ""),
                "language": str(src.get("language") or ""),
                "notes": str(src.get("notes") or ""),
                "status": str(src.get("status") or "Not Started"),
                "rating": int(src.get("rating") or 0),
                "favorite": bool(src.get("favorite")),
                "last_chapter": src.get("last_chapter"),
                "last_read": src.get("last_read"),
                "genres": list(src.get("genres") or genres),
                "tags": list(src.get("tags") or tags),
                "parodies": list(src.get("parodies") or parodies),
            })
            write_sidecar(folder, sc)
    except Exception:
        pass
    return {"ok": True, "series": fresh}


@app.delete("/api/series/{series_id}", dependencies=[Depends(require_token)])
def delete_series(series_id: int) -> dict[str, Any]:
    """Remove a series from the DB (does not delete files from disk)."""
    data = _db.get_series_by_id(series_id)
    if not data:
        raise HTTPException(status_code=404, detail="Series not found")
    _db.delete_series(series_id)
    return {"ok": True, "deleted": series_id}


class SplitChapterIn(BaseModel):
    page: int  # 1-based global page number across all chapters


@app.post("/api/series/{series_id}/split_chapter", dependencies=[Depends(require_token)])
def split_chapter(series_id: int, body: SplitChapterIn) -> dict[str, Any]:
    """Split the chapter at the given global page. Pages from that point onward
    move into a new chapter folder."""
    data = _db.get_series_by_id(series_id)
    if not data:
        raise HTTPException(status_code=404, detail="Series not found")

    folder_path = str(data.get("folder_path") or "")
    root = Path(folder_path)
    if not root.exists() or not root.is_dir():
        raise HTTPException(status_code=400, detail="Series folder does not exist")

    chapters = get_series_chapters(folder_path)
    if not chapters:
        raise HTTPException(status_code=400, detail="No chapters found")

    # Map the global 1-based page to a chapter + local offset.
    cursor = 0
    chapter_idx = None
    split_pos = None  # 0-based index within the chapter's images list
    for ci, ch in enumerate(chapters):
        n = len(ch.images)
        if cursor + n >= body.page:
            chapter_idx = ci
            split_pos = body.page - cursor - 1  # 0-based; this page stays in current chapter
            break
        cursor += n

    if chapter_idx is None:
        raise HTTPException(status_code=400, detail="Page out of range")

    chapter = chapters[chapter_idx]
    if split_pos <= 0 or split_pos >= len(chapter.images) - 1:
        raise HTTPException(
            status_code=400,
            detail="Cannot split at the first or last page of a chapter",
        )

    # The chapter being split must be a real direct subfolder of the series root.
    # (We only touch THIS chapter's folder — other chapters keep their names.)
    root_resolved = root.resolve()
    current_dir = Path(str(chapter.path))
    try:
        if (not current_dir.exists() or not current_dir.is_dir()
                or current_dir.resolve().parent != root_resolved):
            raise HTTPException(
                status_code=400,
                detail="Chapter splitting requires the chapter to be a direct subfolder of the series root",
            )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    # Build a new sibling folder name derived from the chapter being split, so we
    # NEVER rename/clobber other chapters' folders (preserving their custom names).
    # e.g. "Chap 003 - The Reveal" -> "Chap 003 - The Reveal (part 2)".
    base = current_dir.name
    candidate = f"{base} (part 2)"
    new_dir = root / _sanitize_name(candidate)
    attempt = 2
    while new_dir.exists():
        attempt += 1
        new_dir = root / _sanitize_name(f"{base} (part {attempt})")

    # Move pages from split_pos+1 onward into the new folder. Track every move so
    # we can fully roll back (and remove the new dir) if any step fails.
    done: list[tuple[Path, Path]] = []  # (dst, src) pairs already moved
    try:
        new_dir.mkdir(parents=False, exist_ok=False)
        for img_path in chapter.images[split_pos + 1:]:
            src = current_dir / Path(str(img_path)).name
            if not src.exists():
                continue
            dst = new_dir / src.name
            if dst.exists():
                stem, suffix, n = dst.stem, dst.suffix, 1
                while dst.exists():
                    dst = new_dir / f"{stem}_{n}{suffix}"
                    n += 1
            src.rename(dst)
            done.append((dst, src))
    except Exception as exc:
        # Roll back: move every already-relocated file back, then drop the new dir.
        for dst, src in reversed(done):
            try:
                if dst.exists() and not src.exists():
                    dst.rename(src)
            except OSError:
                pass
        try:
            if new_dir.exists() and not any(new_dir.iterdir()):
                new_dir.rmdir()
        except OSError:
            pass
        raise HTTPException(status_code=500, detail=f"Split failed (rolled back): {exc}")

    # Invalidate the cached page count so the library reflects the new structure.
    with _CACHE_LOCK:
        _PAGE_COUNTS.pop(str(series_id), None)
    _invalidate_chapters_cache(folder_path)

    return {"ok": True, "new_chapter": new_dir.name, "moved": len(done)}


class RenameSeriesIn(BaseModel):
    name: str  # new title; also becomes the on-disk folder name


@app.post("/api/series/{series_id}/rename", dependencies=[Depends(require_token)])
def rename_series(series_id: int, body: RenameSeriesIn) -> dict[str, Any]:
    """Rename a series: renames its folder on disk and updates the DB + sidecar.

    The new name becomes both the display title and the folder name. The folder is
    moved within its current parent directory; the rest of the path is unchanged."""
    data = _db.get_series_by_id(series_id)
    if not data:
        raise HTTPException(status_code=404, detail="Series not found")

    new_name = _sanitize_name(body.name)
    if not new_name:
        raise HTTPException(status_code=400, detail="Name is empty or contains only invalid characters")

    old_path = Path(str(data.get("folder_path") or ""))
    if not old_path.exists() or not old_path.is_dir():
        raise HTTPException(status_code=400, detail="Series folder does not exist on disk")
    if not _is_inside_library(old_path):
        raise HTTPException(status_code=403, detail="Series folder is outside the library")

    new_path = old_path.parent / new_name
    # Case-only rename on a case-insensitive FS (Windows): new_path.resolve() ==
    # old_path.resolve(), so a plain rename is a no-op and the casing wouldn't
    # actually change on disk. Detect it by comparing the raw final components.
    case_only = (new_name != old_path.name and new_name.lower() == old_path.name.lower())
    is_noop = (new_path.resolve() == old_path.resolve()) and not case_only

    moved_on_disk = False
    if not is_noop:
        # Reject if another DB row already owns the destination path (would hit the
        # UNIQUE(folder_path) constraint AFTER the disk move, leaving disk/DB split).
        owner = _db.series_id_for_path(str(new_path))
        if owner is not None and owner != series_id:
            raise HTTPException(status_code=409, detail="Another series already uses that folder name")
        if new_path.exists() and not case_only:
            raise HTTPException(status_code=409, detail="A folder with that name already exists here")
        try:
            if case_only:
                # Two-step rename so Windows actually applies the new casing.
                tmp = old_path.parent / f".mgs_rename_{_secrets.token_hex(4)}"
                old_path.rename(tmp)
                tmp.rename(new_path)
            else:
                old_path.rename(new_path)
            moved_on_disk = True
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Folder rename failed: {exc}")

    # From here, the folder is moved on disk. Any DB/sidecar failure must roll the
    # move back so disk and DB never diverge (a divergence would later get the row
    # flagged unavailable / lost).
    try:
        _db.rename_series_folder(series_id, str(new_path))
        fresh = _db.get_series_by_id(series_id) or data
        _db.update_series_metadata(
            series_id=series_id,
            title=new_name,
            series_name=str(fresh.get("series_name") or ""),
            author=str(fresh.get("author") or ""),
            status=str(fresh.get("status") or "Not Started"),
            rating=int(fresh.get("rating") or 0),
            favorite=bool(fresh.get("favorite")),
            notes=str(fresh.get("notes") or ""),
            language=str(fresh.get("language") or ""),
            genres=list(fresh.get("genres") or []),
            tags=list(fresh.get("tags") or []),
        )
    except Exception as exc:
        if moved_on_disk:
            try:
                new_path.rename(old_path)
                _db.rename_series_folder(series_id, str(old_path))
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=f"Rename failed (rolled back): {exc}")

    # Mirror the title into the (now relocated) sidecar so a rescan keeps it.
    try:
        from sidecar import read_sidecar, write_sidecar
        sc = read_sidecar(str(new_path)) or {}
        sc["title"] = new_name
        write_sidecar(str(new_path), sc)
    except Exception:
        pass

    # Invalidate per-series caches that keyed off the old path. The folder moved,
    # so the cached chapters payload (old absolute page paths) is stale — drop it
    # for both old and new paths; the new one rebuilds on next open.
    with _CACHE_LOCK:
        _COVER_PATH_CACHE.pop(series_id, None)
    _invalidate_chapters_cache(str(old_path))
    _invalidate_chapters_cache(str(new_path))

    return {"ok": True, "series": _db.get_series_by_id(series_id)}


class RenameChapterIn(BaseModel):
    old_name: str            # current chapter folder name (relative to series root)
    number: int | None = None  # new chapter number; combined with title
    title: str | None = None   # optional chapter title appended after the number


def _chapter_folder_name(number: int | None, title: str) -> str:
    """Build a chapter folder name from a number and optional title:
      (3, "The Reveal") -> "Chap 003 - The Reveal"
      (3, "")           -> "Chap 003"
      (None, "Extras")  -> "Extras"  (no number => title only)"""
    title = _sanitize_name(title)
    if number is None:
        return title
    base = f"Chap {int(number):03d}"
    return f"{base} - {title}" if title else base


@app.post("/api/series/{series_id}/rename_chapter", dependencies=[Depends(require_token)])
def rename_chapter(series_id: int, body: RenameChapterIn) -> dict[str, Any]:
    """Rename a chapter's folder on disk (number + title combined into the name)."""
    data = _db.get_series_by_id(series_id)
    if not data:
        raise HTTPException(status_code=404, detail="Series not found")

    root = Path(str(data.get("folder_path") or ""))
    if not root.exists() or not root.is_dir():
        raise HTTPException(status_code=400, detail="Series folder does not exist")
    if not _is_inside_library(root):
        raise HTTPException(status_code=403, detail="Series folder is outside the library")

    old_dir = root / Path(str(body.old_name)).name
    # The chapter must be a direct subfolder of the series root.
    if not old_dir.exists() or not old_dir.is_dir() or old_dir.resolve().parent != root.resolve():
        raise HTTPException(status_code=400, detail="Chapter must be a direct subfolder of the series")

    new_name = _chapter_folder_name(body.number, body.title or "")
    if not new_name:
        raise HTTPException(status_code=400, detail="Resulting chapter name is empty")

    new_dir = root / new_name
    # Case-only rename (Windows): resolve() collapses, so use a two-step rename.
    case_only = (new_name != old_dir.name and new_name.lower() == old_dir.name.lower())
    if new_dir.resolve() == old_dir.resolve() and not case_only:
        return {"ok": True, "old_name": old_dir.name, "new_name": new_dir.name}
    if new_dir.exists() and not case_only:
        raise HTTPException(status_code=409, detail="A chapter folder with that name already exists")

    try:
        if case_only:
            tmp = root / f".mgs_rename_{_secrets.token_hex(4)}"
            old_dir.rename(tmp)
            tmp.rename(new_dir)
        else:
            old_dir.rename(new_dir)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Chapter rename failed: {exc}")

    # Carry reading progress over if it pointed at the old chapter name.
    try:
        if str(data.get("last_chapter") or "") == old_dir.name:
            _db.update_last_read_progress(series_id, new_dir.name, int(data.get("last_page") or 0))
    except Exception:
        pass

    with _CACHE_LOCK:
        _PAGE_COUNTS.pop(str(series_id), None)
    _invalidate_chapters_cache(str(root))
    return {"ok": True, "old_name": old_dir.name, "new_name": new_dir.name}


# --------------------------------------------------------- library management --

@app.get("/api/libraries", dependencies=[Depends(require_token_read)])
def list_libraries() -> list[dict[str, Any]]:
    """Configured libraries with name, privacy/default flags, reachability, and
    series count — feeds the settings menu and the floating switcher."""
    out: list[dict[str, Any]] = []
    for lib in _db.get_libraries():
        p = str(lib.get("path") or "")
        try:
            online = Path(p).exists()
        except OSError:
            online = False
        lid = lib.get("id")
        count = len(_db.get_series_list(library_id=lid)) if lid is not None else 0
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


@app.post("/api/libraries/{library_id}", dependencies=[Depends(require_token)])
def update_library(library_id: int, body: LibraryUpdateIn) -> dict[str, Any]:
    """Rename a library or toggle its private flag."""
    name = None if body.name is None else _sanitize_name(body.name) or "Library"
    _db.update_library(library_id, name=name, private=body.private)
    return {"ok": True, "libraries": list_libraries()}


@app.post("/api/libraries/{library_id}/default", dependencies=[Depends(require_token)])
def set_default_library(library_id: int) -> dict[str, Any]:
    """Mark this library as the one the app opens to (when that setting is on)."""
    _db.set_default_library(library_id)
    return {"ok": True, "libraries": list_libraries()}


@app.delete("/api/libraries/{library_id}", dependencies=[Depends(require_token)])
def remove_library(library_id: int) -> dict[str, Any]:
    """Remove a library (and its series records) from the APP ONLY — nothing on
    disk is deleted; re-adding the folder later rescans it back. Refuses to remove
    the last remaining library (the app needs at least one)."""
    libs = _db.get_libraries()
    if not any(int(l.get("id", -1)) == library_id for l in libs):
        raise HTTPException(status_code=404, detail="Library not found")
    if len(libs) <= 1:
        raise HTTPException(status_code=400, detail="Can't remove the last library.")
    removed = _db.remove_library(library_id)
    # Drop caches that may reference the removed series so nothing stale lingers.
    with _CACHE_LOCK:
        _COVER_PATH_CACHE.clear()
    _invalidate_chapters_cache()
    return {"ok": True, "removed_series": removed, "libraries": list_libraries()}


@app.get("/api/browse", dependencies=[Depends(require_token_read)])
def browse_dirs(path: str = Query(default="")) -> dict[str, Any]:
    """List subdirectories of `path` so the UI can pick a library folder. With no
    path, returns the available drive/filesystem roots. Read-only; lists folders
    only (never file contents), so there's no data exposure beyond folder names."""
    # No path → enumerate roots. On Windows that's the mounted drive letters.
    if not path:
        roots: list[dict[str, Any]] = []
        if _os.name == "nt":
            import string
            for letter in string.ascii_uppercase:
                drive = f"{letter}:\\"
                if _os.path.exists(drive):
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


# Background rescan state. One scan at a time; the UI polls /api/rescan/status.
_RESCAN_LOCK = threading.Lock()
_RESCAN_STATE: dict[str, Any] = {"running": False, "done": False, "error": None, "added": None, "series": 0}


def _run_rescan(add_path: str | None, add_name: str | None = None,
                library_id: int | None = None) -> None:
    try:
        if add_path:
            # Adding a new folder always scans just that new library.
            new_id = _db.add_library(add_path, name=add_name)
            library_id = new_id
        from scanner import scan_and_sync
        result = scan_and_sync(_db, library_id=library_id)
        total = sum(len(v) for v in (result or {}).values())
        with _RESCAN_LOCK:
            _RESCAN_STATE.update(running=False, done=True, error=None, series=total)
        # New/renamed folders invalidate cached covers, counts, and chapters.
        with _CACHE_LOCK:
            _COVER_PATH_CACHE.clear()
        _invalidate_chapters_cache()   # all of them — folders may have changed
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


@app.post("/api/rescan", dependencies=[Depends(require_token)])
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


@app.get("/api/rescan/status", dependencies=[Depends(require_token_read)])
def rescan_status() -> dict[str, Any]:
    with _RESCAN_LOCK:
        return dict(_RESCAN_STATE)


# ------------------------------------------------------ import from a link --
def _library_path(library_id: int | None) -> str | None:
    """Resolve a library id to its on-disk root path (or None)."""
    for lib in _db.get_libraries():
        if lib["id"] == library_id:
            return lib["path"]
    return None


@app.get("/api/sources", dependencies=[Depends(require_token_read)])
def sources() -> list[dict[str, str]]:
    """Import adapters currently available (built-in + any local plugins)."""
    return list_sources()


@app.get("/api/search-web", dependencies=[Depends(require_token)])
def search_web(q: str = "", limit: int = 20) -> dict[str, Any]:
    """Search searchable sources (e.g. MangaDex) by title, so a user can find and
    import a series that isn't in their library yet. Returns combined results; pick
    one and import it via the normal /api/scrape flow using its `url`."""
    q = (q or "").strip()
    if not q:
        return {"results": [], "searchable": any_searchable()}
    results = search_all(q, limit=min(int(limit or 20), 50))
    return {"results": results, "searchable": any_searchable()}


@app.get("/api/chapter-count", dependencies=[Depends(require_token_read)])
def chapter_count(source: str = "", url: str = "") -> dict[str, Any]:
    """Readable chapter count for ONE web-search result, resolved on demand. The
    search grid renders immediately and calls this per card so each "· N ch" fills
    in lazily (fetching all counts up front would rate-limit and stall search).
    Returns {count} where count is -1 if unknown/unsupported."""
    from sources.registry import chapter_count_for
    return {"count": chapter_count_for(source, url)}


# ----------------------------------------------- user Source extensions --
# A "Source extension" is a declarative JSON manifest describing how to read a
# manga site. It's interpreted by the safe DeclarativeSource engine — it can never
# run code — so it's safe for users to install/share (see EXTENSIONS_DESIGN.md).

@app.get("/api/sources/extensions", dependencies=[Depends(require_token_read)])
def get_extensions() -> dict[str, Any]:
    """Installed Source extensions + any load errors, for the manage UI."""
    return list_extensions()


class ExtensionIn(BaseModel):
    manifest: dict[str, Any]


@app.post("/api/sources/extensions", dependencies=[Depends(require_token)])
def add_extension(body: ExtensionIn) -> dict[str, Any]:
    """Install (or update) a Source extension from a manifest. Validates first;
    a bad manifest is rejected with the reason and nothing is written."""
    try:
        info = install_extension(body.manifest)
    except Exception as exc:  # ManifestError/ValueError — surface the reason
        raise HTTPException(status_code=400, detail=f"Invalid extension: {exc}")
    return {"ok": True, **info}


class ExtensionToggleIn(BaseModel):
    enabled: bool


@app.post("/api/sources/extensions/{ext_id}/toggle", dependencies=[Depends(require_token)])
def toggle_extension(ext_id: str, body: ExtensionToggleIn) -> dict[str, Any]:
    if not set_extension_enabled(ext_id, body.enabled):
        raise HTTPException(status_code=404, detail="No extension with that id.")
    return {"ok": True, "enabled": body.enabled}


@app.delete("/api/sources/extensions/{ext_id}", dependencies=[Depends(require_token)])
def delete_extension(ext_id: str) -> dict[str, Any]:
    if not remove_extension(ext_id):
        raise HTTPException(status_code=404, detail="No extension with that id.")
    return {"ok": True, "removed": ext_id}


class ExtExportIn(BaseModel):
    url: str            # a series URL on the site the AI already figured out
    name: str | None = None


@app.post("/api/sources/extensions/from-ai", dependencies=[Depends(require_token)])
def export_ai_extension(body: ExtExportIn) -> dict[str, Any]:
    """Turn the AI's cached rules for a site into a shareable Source extension
    manifest (which then works WITHOUT an LLM). Returns the manifest for review;
    the user installs it via the normal install endpoint. 404 if the AI hasn't
    learned this site yet (import it once via the AI first)."""
    from urllib.parse import urlparse as _up
    domain = _up((body.url or "").strip()).netloc
    if not domain:
        raise HTTPException(status_code=400, detail="Not a valid URL.")
    try:
        from sources.ai import manifest_from_ai_rules
        manifest = manifest_from_ai_rules(domain, name=(body.name or "").strip())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Export failed: {exc}")
    if not manifest:
        raise HTTPException(status_code=404,
                            detail="No AI-learned rules for this site yet. Import it once with the AI source first.")
    return {"ok": True, "manifest": manifest}


class ScrapePreviewIn(BaseModel):
    url: str


@app.post("/api/scrape/preview", dependencies=[Depends(require_token)])
def scrape_preview(body: ScrapePreviewIn) -> dict[str, Any]:
    """Resolve a series URL to its metadata + chapter count for confirmation,
    WITHOUT downloading anything."""
    url = (body.url or "").strip()
    src = find_source(url)
    if not src:
        raise HTTPException(status_code=400, detail="No import source recognizes this link.")
    try:
        meta = src.fetch_series(url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not read the series: {exc}")
    external_id = f"{meta.source}:{meta.external_id}"
    # Duplicate detection: if this series was already imported, tell the UI so it
    # can offer "Sync new chapters" instead of a fresh re-download.
    existing = _db.series_by_external_id(external_id)
    already = None
    if existing:
        already = {"id": existing["id"], "title": existing["title"],
                   "library": existing.get("library_name") or ""}
    return {
        "source": src.label,
        "title": meta.title,
        "author": meta.author,
        "genres": meta.genres,
        "chapters": len(meta.chapters),
        "cover_url": meta.cover_url,
        "description": meta.description,
        "external_id": external_id,
        "already": already,
    }


# ---------------------------------------------- read-before-import (preview) --
# Let the user READ a series from a source without importing it: fetch its chapter
# list on demand, and stream one chapter's pages live (proxied through us so the
# source's Referer/headers apply and there are no CORS/hotlink issues). Nothing is
# written to disk unless the user then imports.

@app.post("/api/preview/series", dependencies=[Depends(require_token)])
def preview_series(body: ScrapePreviewIn) -> dict[str, Any]:
    """Full metadata + the CHAPTER LIST for a source URL (no page downloads), so the
    UI can show a browsable chapter list to read before importing."""
    url = (body.url or "").strip()
    src = find_source(url)
    if not src:
        raise HTTPException(status_code=400, detail="No import source recognizes this link.")
    try:
        meta = src.fetch_series(url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not read the series: {exc}")
    external_id = f"{meta.source}:{meta.external_id}"
    existing = _db.series_by_external_id(external_id)
    chapters = [
        {"index": i, "id": c.id, "number": c.number, "title": c.title,
         "language": c.language, "url": c.url}
        for i, c in enumerate(meta.chapters)
    ]
    # A source may resolve chapters from a DIFFERENT adapter than the one that owns
    # the URL (e.g. AniList supplies metadata but hands off to MangaDex for readable
    # pages). The reader must fetch pages from the adapter that produced the chapters
    # (meta.source), so page-fetch (preview/pages) is dispatched there — while the
    # UI badge still shows where the entry was FOUND (src.label).
    page_src = find_by_name(meta.source) if meta.source else None
    return {
        "source": meta.source if page_src else src.name,
        "source_label": src.label,
        "found_via": src.name,
        "url": url,
        "title": meta.title, "author": meta.author, "genres": meta.genres,
        "cover_url": meta.cover_url, "description": meta.description,
        "external_id": external_id,
        "already": ({"id": existing["id"], "title": existing["title"]} if existing else None),
        "chapters": chapters,
    }


class PreviewPagesIn(BaseModel):
    source: str          # adapter name (from preview/series)
    chapter_id: str      # the chapter's source id
    number: str = ""
    title: str = ""


@app.post("/api/preview/pages", dependencies=[Depends(require_token)])
def preview_pages(body: PreviewPagesIn) -> dict[str, Any]:
    """Page image URLs for ONE chapter (fetched live from the source), plus the
    per-source image headers, so the reader can stream that chapter without a
    full import. The URLs are returned as proxy tokens the reader loads via
    /api/preview/page (see below)."""
    src = find_by_name(body.source)
    if not src:
        raise HTTPException(status_code=400, detail="Unknown source.")
    from sources.base import ChapterMeta  # local import — light
    ch = ChapterMeta(id=body.chapter_id, number=body.number, title=body.title)
    try:
        urls = src.fetch_pages(ch)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not read this chapter: {exc}")
    return {"pages": list(urls), "count": len(urls)}


# Cap a proxied page fetch so a bad URL can't stream unbounded into memory.
_MAX_PROXY_BYTES = 40 * 1024 * 1024


@app.get("/api/preview/page", dependencies=[Depends(require_token_read)])
def preview_page(url: str = Query(...), source: str = Query("")):
    """Proxy a single remote page image through the server (applying the source's
    Referer/headers), so the reader can display it via a normal <img> without CORS
    or hotlink problems. SSRF-guarded + size-capped; used only for preview reading."""
    import urllib.request
    # SSRF guard — FAIL CLOSED: if the guard itself can't run, reject rather than
    # fetch. Only proxy public http(s) hosts (no localhost/LAN/metadata).
    try:
        from sources.ai import _is_public_url
    except Exception:
        raise HTTPException(status_code=503, detail="Proxy unavailable (SSRF guard could not load).")
    if not (url.startswith("http") and _is_public_url(url)):
        raise HTTPException(status_code=400, detail="Refusing to fetch that address.")
    headers = {"User-Agent": "MangaShelf/1.0"}
    src = find_by_name(source) if source else None
    if src:
        headers.update(src.image_headers() or {})
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=45) as r:
            data = r.read(_MAX_PROXY_BYTES + 1)
            ctype = r.headers.get("Content-Type", "image/jpeg")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Page fetch failed: {exc}")
    if len(data) > _MAX_PROXY_BYTES:
        raise HTTPException(status_code=413, detail="Page too large.")
    from fastapi.responses import Response
    # private (this is an authenticated response — a shared cache must not serve it
    # to another user), and short-lived (source page URLs like MangaDex at-home
    # tokens rotate, so a long cache could go stale/404).
    return Response(content=data, media_type=ctype,
                    headers={"Cache-Control": "private, max-age=600"})


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
        sid_before = _db.series_id_for_folder(job["folder"])
        if sid_before:
            row_before = _db.get_series_by_id(sid_before)
            with _CACHE_LOCK:
                total_before = _PAGE_COUNTS.get(str(sid_before))
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
                    _db.set_fresh_chapters(sid_before, True)
                except Exception as exc:  # noqa: BLE001
                    print(f"[resync] fresh-chapters flag failed: {exc}")
            # The cached page total is now stale (new pages on disk). Drop it so
            # the next library listing re-counts (via the background warm-up)
            # instead of showing the old total.
            with _CACHE_LOCK:
                _PAGE_COUNTS.pop(str(sid_before), None)
            _save_page_counts()
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
    scan_and_sync(_db, library_id=job["library_id"])
    # For a fresh import, save the source's real cover as the series cover.
    if job["kind"] != "resync":
        try:
            sid = _db.series_id_for_folder(result["series_dir"])
            if sid and getattr(meta, "cover_url", ""):
                _save_source_cover(sid, meta.cover_url, src.image_headers())
        except Exception as exc:  # noqa: BLE001
            print(f"[cover] post-import cover step failed: {exc}")
    with _CACHE_LOCK:
        _COVER_PATH_CACHE.clear()
    _invalidate_chapters_cache()


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


@app.post("/api/scrape", dependencies=[Depends(require_token)])
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


@app.get("/api/scrape/status", dependencies=[Depends(require_token_read)])
def scrape_status() -> dict[str, Any]:
    return _scrape_status()


@app.delete("/api/scrape/{job_id}", dependencies=[Depends(require_token)])
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
        _db.set_series_location(job["series_id"], job["target_lib_id"], str(dst))
        committed = True
        shutil.rmtree(src, ignore_errors=True)
        with _CACHE_LOCK:
            _COVER_PATH_CACHE.clear()
        _invalidate_chapters_cache()
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


@app.post("/api/series/{series_id}/move", dependencies=[Depends(require_token)])
def move_series(series_id: int, body: MoveIn) -> dict[str, Any]:
    data = _db.get_series_by_id(series_id)
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


@app.get("/api/move/status", dependencies=[Depends(require_token_read)])
def move_status() -> dict[str, Any]:
    return _move_status()


@app.delete("/api/move/{job_id}", dependencies=[Depends(require_token)])
def cancel_move(job_id: int) -> dict[str, Any]:
    """Remove a QUEUED move. A running move can't be cancelled (it finishes)."""
    with _MOVE_LOCK:
        for j in _MOVE_JOBS:
            if j["id"] == job_id and j["status"] == "queued":
                _MOVE_JOBS.remove(j)
                return {"ok": True}
    raise HTTPException(status_code=404, detail="No queued move with that id.")


# ------------------------------------------ re-sync a series from its origin --
@app.post("/api/series/{series_id}/resync", dependencies=[Depends(require_token)])
def resync_series(series_id: int) -> dict[str, Any]:
    """Queue a re-sync of this series from its origin (downloads any new
    chapters). Shares the import queue; poll /api/scrape/status for progress."""
    data = _db.get_series_by_id(series_id)
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


@app.post("/api/resync-all", dependencies=[Depends(require_token)])
def resync_all(library: int | None = None) -> dict[str, Any]:
    """Queue a re-sync for EVERY imported series in the library (those with a
    known origin) — the one-click "check everything for new chapters". Series
    whose adapter/folder is unavailable are skipped, as are ones already in the
    queue. Jobs run one at a time on the existing import worker; the UI's usual
    job indicator shows progress."""
    rows = _db.get_series_list(library_id=library)
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


@app.get("/api/settings", dependencies=[Depends(require_token_read)])
def get_settings() -> dict[str, Any]:
    """App preferences, server-stored so they persist across browser-storage
    clears and sync between devices."""
    return _load_settings()


@app.post("/api/settings", dependencies=[Depends(require_token)])
def update_settings(body: dict[str, Any]) -> dict[str, Any]:
    """Merge in changed preference keys (unknown keys ignored)."""
    return _save_settings(body or {})


# ----------------------------------------------------------------- auth API --
class LoginIn(BaseModel):
    password: str


class SetPasswordIn(BaseModel):
    # current is required only when a password is ALREADY set (to change it).
    current: str | None = None
    new: str  # empty string clears the password (reverts to open-on-tailnet)


@app.get("/api/auth-status")
def auth_status() -> JSONResponse:
    """Whether a password gate is configured. No auth required — the login screen
    calls this before the user has any credential."""
    return JSONResponse({"password_set": _password_is_set()})


@app.post("/api/login")
def login(body: LoginIn) -> JSONResponse:
    """Exchange the app password for the API token. No token required (this is how
    you obtain it). The password check uses PBKDF2 (deliberately slow) + a
    constant-time compare, so it's inherently resistant to timing attacks."""
    if not _password_is_set():
        # No password configured → login is meaningless; hand back the token so a
        # misconfigured client still works. (Normally the page just gets the token
        # injected directly in this case.)
        return JSONResponse({"ok": True, "token": WEB_TOKEN})
    if not _password_matches(body.password or ""):
        raise HTTPException(status_code=401, detail="Incorrect password")
    return JSONResponse({"ok": True, "token": WEB_TOKEN})


@app.post("/api/password", dependencies=[Depends(require_token)])
def set_password(body: SetPasswordIn) -> JSONResponse:
    """Set, change, or clear the app password. Requires the token (you must be
    logged in). If a password is already set, `current` must match it."""
    if _password_is_set():
        if not _password_matches(body.current or ""):
            raise HTTPException(status_code=403, detail="Current password is incorrect")
    new = (body.new or "").strip()
    if new and len(new) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters")
    _set_password(new)
    return JSONResponse({"ok": True, "password_set": bool(new)})


@app.get("/api/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True, "libraries": len(_library_roots())})
