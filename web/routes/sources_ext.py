"""Import sources: link import helpers, extensions CRUD, web search, read-before-import preview.

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
from sources.registry import (
    find_source, list_sources, find_by_name, source_info,
    install_extension, remove_extension, set_extension_enabled, list_extensions,
    search_all, any_searchable,
)
from sources.downloader import download_series

router = APIRouter()


# ------------------------------------------------------ import from a link --
def _library_path(library_id: int | None) -> str | None:
    """Resolve a library id to its on-disk root path (or None)."""
    for lib in api._db.get_libraries():
        if lib["id"] == library_id:
            return lib["path"]
    return None


@router.get("/api/sources", dependencies=[Depends(require_token_read)])
def sources() -> list[dict[str, str]]:
    """Import adapters currently available (built-in + any local plugins)."""
    return list_sources()


@router.get("/api/search-web", dependencies=[Depends(require_token)])
def search_web(q: str = "", limit: int = 20) -> dict[str, Any]:
    """Search searchable sources (e.g. MangaDex) by title, so a user can find and
    import a series that isn't in their library yet. Returns combined results; pick
    one and import it via the normal /api/scrape flow using its `url`."""
    q = (q or "").strip()
    if not q:
        return {"results": [], "searchable": any_searchable()}
    results = search_all(q, limit=min(int(limit or 20), 50))
    return {"results": results, "searchable": any_searchable()}


@router.get("/api/chapter-count", dependencies=[Depends(require_token_read)])
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

@router.get("/api/sources/extensions", dependencies=[Depends(require_token_read)])
def get_extensions() -> dict[str, Any]:
    """Installed Source extensions + any load errors, for the manage UI."""
    return list_extensions()


class ExtensionIn(BaseModel):
    manifest: dict[str, Any]


@router.post("/api/sources/extensions", dependencies=[Depends(require_token)])
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


@router.post("/api/sources/extensions/{ext_id}/toggle", dependencies=[Depends(require_token)])
def toggle_extension(ext_id: str, body: ExtensionToggleIn) -> dict[str, Any]:
    if not set_extension_enabled(ext_id, body.enabled):
        raise HTTPException(status_code=404, detail="No extension with that id.")
    return {"ok": True, "enabled": body.enabled}


@router.delete("/api/sources/extensions/{ext_id}", dependencies=[Depends(require_token)])
def delete_extension(ext_id: str) -> dict[str, Any]:
    if not remove_extension(ext_id):
        raise HTTPException(status_code=404, detail="No extension with that id.")
    return {"ok": True, "removed": ext_id}


class ExtExportIn(BaseModel):
    url: str            # a series URL on the site the AI already figured out
    name: str | None = None


@router.post("/api/sources/extensions/from-ai", dependencies=[Depends(require_token)])
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


@router.post("/api/scrape/preview", dependencies=[Depends(require_token)])
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
    existing = api._db.series_by_external_id(external_id)
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

@router.post("/api/preview/series", dependencies=[Depends(require_token)])
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
    existing = api._db.series_by_external_id(external_id)
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


@router.post("/api/preview/pages", dependencies=[Depends(require_token)])
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


@router.get("/api/preview/page", dependencies=[Depends(require_token_read)])
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


