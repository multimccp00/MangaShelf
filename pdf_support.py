"""PDF reading support for MangaShelf.

A PDF chapter is referenced page-by-page with a token of the form
``<absolute-path-to.pdf>#<1-based-page>``. The scanner emits these tokens in a
chapter's page list; the page-serving endpoint renders the requested page to a
JPEG on the fly (cached). This keeps PDFs flowing through the same code paths as
image files without changing the DB schema or the page-list contract.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

PDF_EXTS = {".pdf"}

# PyMuPDF is imported on first use: it costs ~0.5s at startup, which is felt on a
# cold boot, and most launches never touch a PDF.
fitz = None
_PDF_OK = None   # unknown until _fitz() runs


def _fitz():
    """Import PyMuPDF lazily; returns the module or None if unavailable."""
    global fitz, _PDF_OK
    if _PDF_OK is None:
        try:
            import fitz as _f  # PyMuPDF
            fitz, _PDF_OK = _f, True
        except Exception:  # pragma: no cover
            _PDF_OK = False
    return fitz

# How many points-per-inch to render at. 150 DPI is sharp on a phone/grid while
# keeping render time and JPEG size reasonable.
_RENDER_DPI = 150


def pdf_available() -> bool:
    return _fitz() is not None


def is_pdf(path: str | Path) -> bool:
    return Path(path).suffix.lower() in PDF_EXTS


def split_page_token(token: str) -> tuple[str, int | None]:
    """Parse a page token. Returns (path, page_no) where page_no is the 1-based
    PDF page, or None for a plain image path. Splits on the LAST '#' so paths
    that happen to contain '#' still work for the PDF case."""
    s = str(token)
    if "#" in s:
        base, _, frag = s.rpartition("#")
        if frag.isdigit() and is_pdf(base):
            return base, int(frag)
    return s, None


_COUNT_PATH = Path.home() / ".mangashelf" / "pdf_pagecounts.json"
_COUNTS: dict[str, list[int]] = {}   # path -> [mtime_ns, size, pages]
_counts_dirty = False
try:
    _COUNTS = json.loads(_COUNT_PATH.read_text(encoding="utf-8"))
except Exception:
    pass


def save_counts() -> None:
    """Persist new PDF page counts (call once after a scan, not per file)."""
    global _counts_dirty
    if not _counts_dirty:
        return
    _counts_dirty = False
    try:
        tmp = _COUNT_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(_COUNTS)), encoding="utf-8")
        os.replace(tmp, _COUNT_PATH)
    except Exception:
        pass


def page_count(pdf_path: str | Path) -> int:
    """Number of pages in a PDF (0 on any failure). Opening a PDF is slow, so
    counts are cached by path + mtime + size, in memory and on disk."""
    global _counts_dirty
    if _fitz() is None:
        return 0
    key = str(pdf_path)
    try:
        st = os.stat(key)
        sig = [st.st_mtime_ns, st.st_size]
        hit = _COUNTS.get(key)
        if hit and hit[:2] == sig:
            return hit[2]
        with fitz.open(key) as doc:
            n = len(doc)
        _COUNTS[key] = sig + [n]
        _counts_dirty = True
        return n
    except Exception:
        return 0


def page_tokens(pdf_path: str | Path) -> list[str]:
    """Page tokens for every page of a PDF: ['<path>#1', '<path>#2', ...]."""
    n = page_count(pdf_path)
    p = str(pdf_path)
    return [f"{p}#{i}" for i in range(1, n + 1)]


def render_page_to_jpeg_bytes(pdf_path: str | Path, page_no: int, dpi: int = _RENDER_DPI) -> bytes:
    """Render a 1-based PDF page to JPEG bytes. Raises on failure."""
    if _fitz() is None:
        raise RuntimeError("PyMuPDF (pymupdf) is not installed")
    with fitz.open(str(pdf_path)) as doc:
        idx = max(0, min(len(doc) - 1, page_no - 1))
        page = doc.load_page(idx)
        zoom = dpi / 72.0
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return pix.tobytes("jpeg", jpg_quality=88)
