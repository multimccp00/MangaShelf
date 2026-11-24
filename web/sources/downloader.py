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

import json
import os
import random
import re
import shutil
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .base import ChapterMeta, MangaSource, SeriesMeta

CHAPTER_INFO = "chapter.json"   # per-chapter {"language": ...}; not an image, scanner ignores it
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
    return name[:150].rstrip(". ") or fallback   # truncation can expose a trailing space


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
    # Prefix the volume when the source knows it, so a series collected into
    # volumes reads as such in the library. Left off entirely when unknown (most
    # sources), keeping existing folder names unchanged. MangaDex reports loose
    # chapters as volume "0", which is not a volume — no prefix for those either.
    vol = (getattr(ch, "volume", "") or "").strip()
    if vol and vol.strip("0."):
        try:
            vol = f"{int(float(vol)):02d}"
        except ValueError:
            pass
        base = f"Vol {vol} - {base}"
    if ch.title:
        base = f"{base} - {_safe_name(ch.title)}"
    return _safe_name(base)


_NUM_RE = re.compile(r"\bchap(?:ter)?\.?\s*(\d+(?:\.\d+)?)", re.I)


def _num_key(num: str) -> str:
    """'0070.1' / '70.10' / '70.1' -> '70.1', so differently padded names compare equal."""
    try:
        return f"{float(num):g}"
    except (TypeError, ValueError):
        return ""


def existing_chapter_numbers(series_dir: Path) -> set[str]:
    """Chapter numbers already on disk, read from folder names. Folder naming has
    changed over time (a "Vol NN - " prefix, chapter titles, other languages), so
    matching by exact folder name re-downloaded whole series; match by number."""
    out: set[str] = set()
    if series_dir.is_dir():
        for p in series_dir.iterdir():
            try:
                m = _NUM_RE.search(p.name) if p.is_dir() else None
                if m and any(p.iterdir()):
                    out.add(_num_key(m.group(1)))
            except OSError:
                continue
    return out


def existing_chapter_dirs(series_dir: Path) -> dict[str, Path]:
    """Chapter number -> its folder on disk (non-empty folders only)."""
    out: dict[str, Path] = {}
    if series_dir.is_dir():
        for p in series_dir.iterdir():
            try:
                m = _NUM_RE.search(p.name) if p.is_dir() else None
                if m and any(p.iterdir()):
                    out.setdefault(_num_key(m.group(1)), p)
            except OSError:
                continue
    return out


def chapter_dir_language(d: Path) -> str:
    try:
        return str(json.loads((d / CHAPTER_INFO).read_text(encoding="utf-8")).get("language") or "")
    except Exception:
        return ""


# Replaced non-English chapters are moved here, never deleted.
REPLACED_DIR = Path.home() / ".mangashelf" / "replaced_chapters"


def already_have(ch: ChapterMeta, folder: str, series_dir: Path, have_nums: set[str]) -> bool:
    if ch.number and _num_key(ch.number) in have_nums:
        return True
    try:
        d = series_dir / folder
        return d.is_dir() and any(d.iterdir())
    except OSError:
        return False


def _ext_from_url(url: str) -> str:
    ext = os.path.splitext(url.split("?", 1)[0])[1].lower()
    return ext if ext in (".jpg", ".jpeg", ".png", ".webp", ".gif") else ".jpg"


def _download(url: str, dest: Path, headers: dict[str, str]) -> int:
    last: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA, **headers})
            try:
                resp = urllib.request.urlopen(req, timeout=60)
            except urllib.error.HTTPError as he:
                # Some CDNs 403 a hotlink Referer instead of requiring one; retry bare.
                if he.code != 403 or not headers:
                    raise
                resp = urllib.request.urlopen(
                    urllib.request.Request(url, headers={"User-Agent": _UA}), timeout=60)
            with resp as r:
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
            "alt_titles": [a for a in (getattr(meta, "alt_titles", None) or []) if a != meta.title],
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
    have_nums = existing_chapter_numbers(series_dir)
    have_dirs = existing_chapter_dirs(series_dir)
    replaced = 0
    for ci, ch in enumerate(meta.chapters, 1):
        ch_dir = series_dir / _chapter_folder(ch, ci)
        final_dir = ch_dir
        # Official English has appeared for a chapter we hold in another language
        # (PT / PT-BR / raw, machine-translated or not): fetch the English into a
        # side folder, and only once it fully downloads swap it in. The old folder
        # is moved to REPLACED_DIR, not deleted. Unknown-language folders (no
        # chapter.json, i.e. older downloads) are left alone.
        old_dir = have_dirs.get(_num_key(ch.number)) if ch.number else None
        upgrade = (getattr(ch, "language", "") == "en" and old_dir is not None
                   and chapter_dir_language(old_dir) not in ("", "en"))
        if upgrade:
            ch_dir = series_dir / (final_dir.name + " (english, downloading)")
        # ponytail: a chapter folder with any file on disk is assumed complete —
        # skip its page-list fetch entirely so re-sync only hits the network for
        # new chapters, not every chapter already downloaded. Repairing a chapter
        # that partially failed needs a manual re-download (delete the folder).
        elif already_have(ch, ch_dir.name, series_dir, have_nums):
            if progress:
                progress(ci, total_ch, _chapter_folder(ch, ci), new_pages)
            continue
        try:
            pages = source.fetch_pages(ch)
        except Exception as exc:  # noqa: BLE001
            print(f"[downloader] chapter {ch.number or ci} page-list failed: {exc}")
            failed_chapters += 1
            if progress:
                progress(ci, total_ch, _chapter_folder(ch, ci), new_pages)
            continue
        if not pages:   # a chapter with no readable pages is a failure, not a clean success
            print(f"[downloader] chapter {ch.number or ci}: no pages found")
            failed_chapters += 1
            if progress:
                progress(ci, total_ch, _chapter_folder(ch, ci), new_pages)
            continue
        ch_dir.mkdir(exist_ok=True)
        # Record the chapter's language (EN / PT / PT-BR / raw ja…) so the library
        # can badge it and the translator knows which chapters are raw.
        if getattr(ch, "language", ""):
            try:
                (ch_dir / CHAPTER_INFO).write_text(json.dumps({"language": ch.language}), encoding="utf-8")
            except OSError:
                pass

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
        if upgrade:
            if "fail" in outcomes:
                # Drop the half-downloaded side folder (only ever created by us,
                # just above) so it doesn't show up as a duplicate chapter.
                print(f"[downloader] English chapter {ch.number} incomplete; keeping the old one")
                shutil.rmtree(ch_dir, ignore_errors=True)
            else:
                try:
                    dest = REPLACED_DIR / _safe_name(series_dir.name) / old_dir.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists():
                        dest = dest.with_name(dest.name + f" {int(time.time())}")
                    shutil.move(str(old_dir), str(dest))
                    target = final_dir if not final_dir.exists() else series_dir / (final_dir.name + " (en)")
                    ch_dir.rename(target)
                    replaced += 1
                    print(f"[downloader] chapter {ch.number}: replaced {old_dir.name!r} with official English")
                except OSError as exc:
                    print(f"[downloader] chapter {ch.number}: swap to English failed: {exc}")
        if progress:
            progress(ci, total_ch, _chapter_folder(ch, ci), new_pages)
        time.sleep(_CH_DELAY)

    return {
        "series_dir": str(series_dir), "chapters": total_ch, "new_pages": new_pages,
        "failed_pages": failed_pages, "failed_chapters": failed_chapters,
        "replaced": replaced,
    }
