from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pdf_support

if TYPE_CHECKING:
    from lists import GlobalLists

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".jpe",
    ".jfif",
    ".png",
    ".webp",
    ".bmp",
    ".gif",
    ".tif",
    ".tiff",
    ".psd",
    ".psb",
}
SKIP_DIR_NAMES = {"$recycle.bin", "system volume information", "__pycache__"}
# Matches names like: Chap 01, Chapter 3, Vol.2, Vol 002, Ep 5, Episode 12, Ch.4
_CHAPTER_NAME_RE = re.compile(
    r"^chap(?:ter)?[\s._-]*\d",
    re.IGNORECASE,
)


def _looks_like_chapter(name: str) -> bool:
    return bool(_CHAPTER_NAME_RE.match(name))


@dataclass
class ScannedChapter:
    name: str
    path: str
    images: list[str]


@dataclass
class ScannedSeries:
    title: str
    folder_path: str
    chapters: list[ScannedChapter]


def natural_key(text: str) -> list[object]:
    return [int(chunk) if chunk.isdigit() else chunk.lower() for chunk in re.split(r"(\d+)", text)]


def is_image(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS


def list_images_in_dir(directory: Path) -> list[str]:
    if not directory.exists() or not directory.is_dir():
        return []
    try:
        images = [p for p in directory.iterdir() if is_image(p)]
    except OSError:
        return []
    images.sort(key=lambda p: natural_key(p.name))
    return [str(p) for p in images]


def list_pdfs_in_dir(directory: Path) -> list[Path]:
    """PDF files directly inside `directory`, natural-sorted by name."""
    if not directory.exists() or not directory.is_dir():
        return []
    try:
        pdfs = [p for p in directory.iterdir() if p.is_file() and pdf_support.is_pdf(p)]
    except OSError:
        return []
    pdfs.sort(key=lambda p: natural_key(p.name))
    return pdfs


def dir_has_content(directory: Path) -> bool:
    """True if a directory directly contains images OR PDFs (i.e. readable as a
    chapter). Used by discovery to recognize a folder as a series/chapter."""
    return bool(list_images_in_dir(directory)) or bool(list_pdfs_in_dir(directory))


def get_first_image_path(directory: str | Path) -> str | None:
    path = Path(directory)
    if not path.exists() or not path.is_dir():
        return None

    # Allow a sidecar override for custom covers.
    try:
        from sidecar import read_sidecar

        sc = read_sidecar(path)
        rel_cover = str((sc or {}).get("cover_image", "")).strip()
        if rel_cover:
            preferred = path / rel_cover
            if preferred.exists() and preferred.is_file() and preferred.suffix.lower() in IMAGE_EXTENSIONS:
                return str(preferred)
    except Exception:
        pass

    best_file: Path | None = None
    best_key: list[object] | None = None
    try:
        for entry in path.iterdir():
            if not is_image(entry):
                continue
            key = natural_key(entry.name)
            if best_key is None or key < best_key:
                best_key = key
                best_file = entry
    except OSError:
        return None
    if best_file:
        return str(best_file)

    # No image directly here. Fall back to a PDF's first page in this folder
    # (returns a '<pdf>#1' page token the cover endpoint knows how to render).
    direct_pdfs = list_pdfs_in_dir(path)
    if direct_pdfs:
        toks = pdf_support.page_tokens(direct_pdfs[0])
        if toks:
            return toks[0]

    # Fallback: some library entries represent a container folder; find first
    # image OR PDF page recursively.
    for current_root, dirs, _ in os.walk(path, topdown=True):
        root_path = Path(current_root)
        dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in SKIP_DIR_NAMES]
        images = list_images_in_dir(root_path)
        if images:
            return images[0]
        pdfs = list_pdfs_in_dir(root_path)
        if pdfs:
            toks = pdf_support.page_tokens(pdfs[0])
            if toks:
                return toks[0]
    return None


def find_chapter_dirs(series_dir: Path) -> list[Path]:
    chapter_dirs: list[Path] = []
    for current_root, dirs, _ in os.walk(series_dir, topdown=True):
        root_path = Path(current_root)
        # Skip hidden metadata/system folders to reduce scan errors and noise.
        dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in SKIP_DIR_NAMES]
        if root_path != series_dir and dir_has_content(root_path):
            chapter_dirs.append(root_path)
            # Treat the first folder that directly contains images/PDFs as the
            # chapter root. Pruning avoids adding nested folders as duplicates.
            dirs.clear()
            continue
    chapter_dirs.sort(key=lambda p: natural_key(p.relative_to(series_dir).as_posix()))
    return chapter_dirs


def discover_series_dirs(root: Path) -> list[Path]:
    discovered: list[Path] = []
    for current_root, dirs, _ in os.walk(root, topdown=True):
        root_path = Path(current_root)
        dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in SKIP_DIR_NAMES]

        if dir_has_content(root_path):
            # This folder directly contains images/PDFs — it's a series.
            # Prune subdirs so its chapter subfolders aren't also discovered as separate series.
            discovered.append(root_path)
            dirs.clear()
        else:
            # No direct content. Only treat this folder as a single series (with
            # subfolders as chapters) when the subfolders themselves look like
            # chapters (e.g. "Ch.01", "Vol.2", "Episode 3", purely numeric names).
            # If the subfolders have arbitrary titles they are separate series —
            # keep walking so each one gets discovered independently.
            content_subdirs = [d for d in dirs if dir_has_content(root_path / d)]
            chapter_named_subdirs = [d for d in content_subdirs if _looks_like_chapter(d)]
            # Only collapse into one series when subfolders are named like chapters.
            if chapter_named_subdirs:
                discovered.append(root_path)
                dirs.clear()  # subdirs are chapters, not separate series

    discovered.sort(key=lambda p: natural_key(str(p.relative_to(root).as_posix())))
    return discovered


def _pages_for_chapter_dir(chapter_dir: Path) -> list[str]:
    """Page tokens for a chapter folder: its image files, plus a flattened page
    list for any PDFs inside it (each PDF page becomes one token)."""
    pages: list[str] = list_images_in_dir(chapter_dir)
    for pdf in list_pdfs_in_dir(chapter_dir):
        pages.extend(pdf_support.page_tokens(pdf))
    return pages


def detect_series(series_dir: Path) -> ScannedSeries:
    chapters: list[ScannedChapter] = []

    # Prefer explicit chapter folders when they exist (these may hold images
    # and/or PDFs).
    chapter_dirs = find_chapter_dirs(series_dir)
    for chapter_dir in chapter_dirs:
        pages = _pages_for_chapter_dir(chapter_dir)
        if not pages:
            continue
        rel = chapter_dir.relative_to(series_dir).as_posix()
        chapters.append(ScannedChapter(name=rel, path=str(chapter_dir), images=pages))

    # Content sitting directly in the series root.
    direct_images = list_images_in_dir(series_dir)
    direct_pdfs = list_pdfs_in_dir(series_dir)

    # Each PDF directly in the series root is its own chapter (named by filename).
    for pdf in direct_pdfs:
        tokens = pdf_support.page_tokens(pdf)
        if not tokens:
            continue
        chapters.append(ScannedChapter(name=pdf.stem, path=str(pdf), images=tokens))

    # Loose images in the series root (no chapter folders) → a single "Chapter 1".
    # Only when there were no chapter folders, mirroring the original behaviour.
    if direct_images and not chapter_dirs:
        chapters.append(
            ScannedChapter(name="Chapter 1", path=str(series_dir), images=direct_images)
        )

    chapters.sort(key=lambda c: natural_key(c.name))
    return ScannedSeries(title=series_dir.name, folder_path=str(series_dir), chapters=chapters)


def scan_library_root(root_path: str) -> list[ScannedSeries]:
    root = Path(root_path)
    if not root.exists() or not root.is_dir():
        return []

    found: list[ScannedSeries] = []
    series_dirs = discover_series_dirs(root)

    for series_dir in series_dirs:
        series = detect_series(series_dir)
        if series.chapters:
            found.append(series)
    return found


def scan_and_sync(database: object, global_lists: "GlobalLists | None" = None,
                  library_id: int | None = None) -> dict[int, list[ScannedSeries]]:  # type: ignore[misc]
    """Scan libraries and sync series into the database.

    If `library_id` is given, only that one library is scanned (the rest are left
    untouched) — used for per-library rescans. Otherwise all libraries are scanned.
    """
    libraries = database.get_libraries()
    if library_id is not None:
        libraries = [lib for lib in libraries if int(lib["id"]) == int(library_id)]
    result: dict[int, list[ScannedSeries]] = {}
    for lib in libraries:
        lib_id = int(lib["id"])
        path = str(lib["path"])
        try:
            series_list = scan_library_root(path)
        except OSError:
            series_list = []
        result[lib_id] = series_list
        for series in series_list:
            # Per-series isolation: the user can delete a series/library (or a
            # sidecar can be malformed) WHILE this scan runs. One bad series must
            # skip, not abort the whole scan mid-transaction — an aborted scan
            # previously surfaced as an opaque "FOREIGN KEY constraint failed"
            # with every remaining library left unscanned.
            try:
                from sidecar import read_sidecar
                sc = read_sidecar(series.folder_path)
                title = sc.get("title", series.title) if sc else series.title
                sid = database.upsert_series(lib_id, series.folder_path, title)
                if sc:
                    database.sync_series_from_sidecar(sid, sc)
                    if global_lists is not None:
                        global_lists.add_genres(sc.get("genres", []))
                        global_lists.add_tags(sc.get("tags", []))
            except Exception as exc:  # noqa: BLE001
                print(f"[scan] skipped {series.folder_path!r}: {exc}")
                continue
        # Drop rows for folders that still exist but are no longer valid series
        # (e.g. the library root once its content moved into sub-series). Only do
        # this when the library root is reachable AND the scan returned something,
        # so a transiently-empty/offline scan can't wipe the library.
        try:
            root_ok = Path(path).exists()
        except OSError:
            root_ok = False
        if root_ok and series_list and hasattr(database, "prune_stale_series"):
            valid = {s.folder_path for s in series_list}
            try:
                database.prune_stale_series(lib_id, valid)
            except Exception as exc:  # noqa: BLE001 — same isolation as above
                print(f"[scan] prune skipped for library {lib_id}: {exc}")
    # Remove (soft-delete) any series whose folder no longer exists. Drive-aware,
    # so it's safe even for a single-library rescan.
    database.purge_missing_series()
    return result


def get_series_chapters(series_path: str) -> list[ScannedChapter]:
    series_dir = Path(series_path)
    if not series_dir.exists() or not series_dir.is_dir():
        return []
    return detect_series(series_dir).chapters
