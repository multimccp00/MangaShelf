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
    "deleteFromDisk": False,     # pre-check "also delete files" in the delete dialog
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
def delete_series(series_id: int, disk: bool = False) -> dict[str, Any]:
    """Remove a series from the DB. With ?disk=true, ALSO sends its folder to the
    recycle bin (never a hard rm — recoverable from the bin). The folder must
    resolve inside a configured library root — same sandbox rule as /api/page —
    so a corrupted/stale folder_path can never delete something outside a library."""
    data = _db.get_series_by_id(series_id)
    if not data:
        raise HTTPException(status_code=404, detail="Series not found")
    disk_result = "kept"
    if disk:
        folder = Path(str(data.get("folder_path") or ""))
        try:
            resolved = folder.resolve()
        except OSError:
            resolved = folder
        if not _is_inside_library(resolved):
            raise HTTPException(status_code=400, detail="Series folder is outside every library root; refusing to delete it.")
        if resolved.is_dir():
            try:
                import send2trash
                send2trash.send2trash(str(resolved))
                disk_result = "recycled"
            except Exception as exc:  # noqa: BLE001 — DB row survives if disk fails
                raise HTTPException(status_code=500, detail=f"Couldn't move the folder to the recycle bin: {exc}")
        else:
            disk_result = "already-gone"
    _db.delete_series(series_id)
    with _CACHE_LOCK:
        _COVER_PATH_CACHE.pop(str(series_id), None)
    _invalidate_chapters_cache(str(data.get("folder_path") or "") or None)
    return {"ok": True, "deleted": series_id, "disk": disk_result}


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


# Library management + browse + rescan live in routes/libraries.py (included
# at the bottom of this file).


# Import-source routes (extensions, web search, preview) live in
# routes/sources_ext.py; the import/move/resync job queue in routes/jobs.py.


# Settings + auth API + health live in routes/settings_auth.py.


# ---------------------------------------------------------------- routers --
# Split-out route modules. Included LAST so everything they import from this
# module (DB proxy, auth deps, caches) is already defined; see routes/__init__.
from routes.libraries import router as _libraries_router  # noqa: E402
from routes.settings_auth import router as _settings_auth_router  # noqa: E402
from routes.sources_ext import router as _sources_ext_router  # noqa: E402
from routes.jobs import router as _jobs_router  # noqa: E402
app.include_router(_libraries_router)
app.include_router(_settings_auth_router)
app.include_router(_sources_ext_router)
app.include_router(_jobs_router)
