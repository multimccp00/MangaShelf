"""PDF reading support for MangaShelf.

A PDF chapter is referenced page-by-page with a token of the form
``<absolute-path-to.pdf>#<1-based-page>``. The scanner emits these tokens in a
chapter's page list; the page-serving endpoint renders the requested page to a
JPEG on the fly (cached). This keeps PDFs flowing through the same code paths as
image files without changing the DB schema or the page-list contract.
"""
from __future__ import annotations

from pathlib import Path

PDF_EXTS = {".pdf"}

try:
    import fitz  # PyMuPDF
    _PDF_OK = True
except Exception:  # pragma: no cover
    _PDF_OK = False

# How many points-per-inch to render at. 150 DPI is sharp on a phone/grid while
# keeping render time and JPEG size reasonable.
_RENDER_DPI = 150


def pdf_available() -> bool:
    return _PDF_OK


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


def page_count(pdf_path: str | Path) -> int:
    """Number of pages in a PDF (0 on any failure)."""
    if not _PDF_OK:
        return 0
    try:
        with fitz.open(str(pdf_path)) as doc:
            return len(doc)
    except Exception:
        return 0


def page_tokens(pdf_path: str | Path) -> list[str]:
    """Page tokens for every page of a PDF: ['<path>#1', '<path>#2', ...]."""
    n = page_count(pdf_path)
    p = str(pdf_path)
    return [f"{p}#{i}" for i in range(1, n + 1)]


def render_page_to_jpeg_bytes(pdf_path: str | Path, page_no: int, dpi: int = _RENDER_DPI) -> bytes:
    """Render a 1-based PDF page to JPEG bytes. Raises on failure."""
    if not _PDF_OK:
        raise RuntimeError("PyMuPDF (pymupdf) is not installed")
    with fitz.open(str(pdf_path)) as doc:
        idx = max(0, min(len(doc) - 1, page_no - 1))
        page = doc.load_page(idx)
        zoom = dpi / 72.0
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return pix.tobytes("jpeg", jpg_quality=88)
