"""Shared download/ingest pipeline used by every source adapter.

Given a resolved SeriesMeta, it downloads each chapter's pages into the library
folder layout the scanner expects, and writes a metadata.json sidecar so a
subsequent rescan picks up title/author/genres/etc. automatically:

    <library>/<Series Title>/Chapter NNNN[ - Title]/PPP.<ext>
    <library>/<Series Title>/metadata.json

Polite (rate-limited), resumable (skips files already on disk), and fault-tolerant
(a failed page is logged and skipped, not fatal).
"""
from __future__ import annotations

import os
import random
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .base import ChapterMeta, MangaSource, SeriesMeta

_UA = "MangaShelf/1.0 (+https://github.com/; self-hosted personal library)"
_CH_DELAY = 0.5         # seconds between chapters (politeness per host)
_CONCURRENCY = 4        # pages downloaded in parallel per chapter
_RETRIES = 3
_MAX_PAGE_BYTES = 100 * 1024 * 1024   # hard cap per page: a page image over 100 MB
                                      # is almost certainly a wrong/hostile URL — reject
                                      # rather than read an unbounded body into memory.
_ILLEGAL = '<>:"/\\|?*'  # characters not allowed in Windows path components


def _safe_name(name: str, fallback: str = "Untitled") -> str:
    name = (name or "").strip()
    for ch in _ILLEGAL:
        name = name.replace(ch, " ")
    name = " ".join(name.split()).rstrip(". ")   # collapse spaces, no trailing dot/space
    return name[:150] or fallback


def _chapter_folder(ch: ChapterMeta, index: int) -> str:
    """Zero-padded, sortable, human-readable chapter folder name."""
    if ch.number:
        # Pad the integer part so "Chapter 0002" sorts before "Chapter 0010".
        try:
            head, _, tail = ch.number.partition(".")
            num = f"{int(head):04d}" + (f".{tail}" if tail else "")
        except ValueError:
            num = ch.number
        base = f"Chapter {num}"
    else:
        base = f"Chapter {index:04d}"
    if ch.title:
        base = f"{base} - {_safe_name(ch.title)}"
    return _safe_name(base)


def _ext_from_url(url: str) -> str:
    ext = os.path.splitext(url.split("?", 1)[0])[1].lower()
    return ext if ext in (".jpg", ".jpeg", ".png", ".webp", ".gif") else ".jpg"


def _download(url: str, dest: Path, headers: dict[str, str]) -> int:
    last: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA, **headers})
            with urllib.request.urlopen(req, timeout=60) as r:
                # Read at most the cap + 1 byte: if the response overflows, it's a
                # bad/hostile URL — bail instead of buffering gigabytes into memory.
                data = r.read(_MAX_PAGE_BYTES + 1)
            if len(data) > _MAX_PAGE_BYTES:
                raise ValueError(f"page exceeds {_MAX_PAGE_BYTES // (1024*1024)} MB cap")
            tmp = dest.with_suffix(dest.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(dest)   # atomic: a crash mid-write never leaves a half file
            return len(data)
        except Exception as exc:  # noqa: BLE001
            last = exc
            # Backoff with jitter so N concurrent workers don't retry in lockstep
            # and thundering-herd a struggling CDN.
            time.sleep(1.0 * (attempt + 1) + random.uniform(0, 0.5))
    raise last if last else RuntimeError("download failed")


def download_series(
    source: MangaSource,
    meta: SeriesMeta,
    library_root: str | Path,
    progress=None,
    dest_dir: str | Path | None = None,
) -> dict:
    """Download the whole series into library_root. `progress(done_ch, total_ch,
    chapter_label, new_pages)` is called after each chapter, if given.

    Pass `dest_dir` to write into an exact existing folder (used by re-sync, so new
    chapters land in the series' current folder and existing pages are skipped)."""
    series_dir = Path(dest_dir) if dest_dir else Path(library_root) / _safe_name(meta.title)
    series_dir.mkdir(parents=True, exist_ok=True)

    # Write the metadata sidecar so the rescan ingests real metadata (not just the
    # folder name). external_id records source+id for future re-sync of new chapters.
    try:
        from sidecar import write_sidecar  # reuse the app's existing sidecar system
        write_sidecar(series_dir, {
            "title": meta.title,
            "author": meta.author,
            "genres": list(meta.genres or []),
            "tags": list(meta.tags or []),
            "parodies": list(getattr(meta, "parodies", None) or []),
            "notes": meta.description,
            "external_id": f"{meta.source}:{meta.external_id}",
        })
    except Exception as exc:  # metadata is a nice-to-have; don't abort the download
        print(f"[downloader] could not write sidecar: {exc}")

    headers = source.image_headers()
    total_ch = len(meta.chapters)
    new_pages = 0
    failed_pages = 0
    failed_chapters = 0
    for ci, ch in enumerate(meta.chapters, 1):
        try:
            pages = source.fetch_pages(ch)
        except Exception as exc:  # noqa: BLE001
            print(f"[downloader] chapter {ch.number or ci} page-list failed: {exc}")
            failed_chapters += 1
            if progress:
                progress(ci, total_ch, _chapter_folder(ch, ci), new_pages)
            continue
        ch_dir = series_dir / _chapter_folder(ch, ci)
        ch_dir.mkdir(exist_ok=True)

        def _one(item):
            pi, url = item
            dest = ch_dir / f"{pi:03d}{_ext_from_url(url)}"
            if dest.exists() and dest.stat().st_size > 0:
                return "skip"  # resume: already have it
            try:
                _download(url, dest, headers)
                return "ok"
            except Exception as exc:  # noqa: BLE001
                print(f"[downloader] page {pi} of chapter {ch.number or ci} failed: {exc}")
                return "fail"

        # Download the chapter's pages a few at a time. Bounded concurrency is the
        # throttle (polite enough), and we still pause between chapters.
        with ThreadPoolExecutor(max_workers=_CONCURRENCY) as ex:
            outcomes = list(ex.map(_one, enumerate(pages, 1)))
        new_pages += outcomes.count("ok")
        failed_pages += outcomes.count("fail")
        if progress:
            progress(ci, total_ch, _chapter_folder(ch, ci), new_pages)
        time.sleep(_CH_DELAY)

    return {
        "series_dir": str(series_dir), "chapters": total_ch, "new_pages": new_pages,
        "failed_pages": failed_pages, "failed_chapters": failed_chapters,
    }
