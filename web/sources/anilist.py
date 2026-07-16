"""AniList search + metadata adapter (with MangaDex hand-off for reading).

AniList (https://anilist.co) has an OFFICIAL, stable public GraphQL API and a huge,
well-curated catalog with excellent ENGLISH titles — which is exactly what plain
title search on other sources gets wrong (e.g. "my dress up darling" instead of the
romaji "Sono Bisque Doll wa Koi o Suru").

But AniList is a *tracking* site: it holds metadata (titles, cover, author, genres,
description, year) — it does NOT host page images, so it can't be read from directly.
So this adapter does two things:

  * search()        -> rich, English-first results (isAdult:false only — SFW).
  * fetch_series()  -> resolves the AniList entry's metadata, then RE-USES MangaDex
                       to supply the readable chapter list (page images), matched by
                       title. If no MangaDex match is found the series still imports,
                       just with an empty chapter list (metadata-only).

This keeps AniList as the good "find it" source and MangaDex as the "read it" source,
without committing any scraping code.
"""
from __future__ import annotations

import json
import re
import urllib.request

from .base import ChapterMeta, MangaSource, SearchResult, SeriesMeta

_GRAPHQL = "https://graphql.anilist.co"
_UA = "MangaShelf/1.0 (+https://github.com/; self-hosted personal library)"
_MAX_JSON_BYTES = 8 * 1024 * 1024
_URL_RE = re.compile(r"anilist\.co/manga/(\d+)")


_SEARCH_QUERY = """
query ($s: String, $n: Int) {
  Page(perPage: $n) {
    media(search: $s, type: MANGA, isAdult: false, sort: SEARCH_MATCH) {
      id
      title { romaji english native }
      description(asHtml: false)
      coverImage { large }
      startDate { year }
      genres
      staff(perPage: 1) { edges { role node { name { full } } } }
    }
  }
}
""".strip()

_DETAIL_QUERY = """
query ($id: Int) {
  Media(id: $id, type: MANGA) {
    id
    title { romaji english native }
    description(asHtml: false)
    coverImage { large }
    genres
    staff(perPage: 4) { edges { role node { name { full } } } }
  }
}
""".strip()


def _post(query: str, variables: dict) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        _GRAPHQL,
        data=body,
        headers={"User-Agent": _UA, "Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read(_MAX_JSON_BYTES + 1)
    if len(raw) > _MAX_JSON_BYTES:
        raise ValueError("AniList response exceeds size cap")
    data = json.loads(raw.decode("utf-8", "replace"))
    if data.get("errors"):
        raise ValueError(f"AniList error: {data['errors']}")
    return data.get("data", {}) or {}


def _clean_desc(html: str) -> str:
    """AniList descriptions carry a little markup even with asHtml:false — strip it."""
    if not html:
        return ""
    text = re.sub(r"<br\s*/?>", " ", html)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _author_from_staff(staff: dict) -> str:
    edges = (staff or {}).get("edges", []) or []
    # Prefer a "Story" credit, else the first listed.
    story = ""
    first = ""
    for e in edges:
        name = (((e or {}).get("node") or {}).get("name") or {}).get("full") or ""
        role = (e or {}).get("role") or ""
        if not first:
            first = name
        if "story" in role.lower() and not story:
            story = name
    return story or first


class AniListSource(MangaSource):
    name = "anilist"
    label = "AniList"
    example = "https://anilist.co/manga/<id>"
    # Not shown in combined web search: AniList hosts no pages (it borrows MangaDex's
    # possibly-incomplete chapters), so its cards only duplicated the English-titled
    # results a full-library source already gives. It stays importable by pasting an
    # anilist.co/manga/<id> URL (matches()/fetch_series() below still work), and its
    # English→romaji title resolution is reused by other sources for matching.
    can_search = False

    def matches(self, url: str) -> bool:
        return "anilist.co/manga/" in (url or "") and bool(_URL_RE.search(url or ""))

    def url_for(self, external_id: str) -> str:
        return f"https://anilist.co/manga/{external_id}" if external_id else ""

    # -- search -------------------------------------------------------------
    def search(self, query: str, limit: int = 20) -> list[SearchResult]:
        query = (query or "").strip()
        if not query:
            return []
        try:
            data = _post(_SEARCH_QUERY, {"s": query, "n": min(int(limit), 25)})
        except Exception:
            return []
        out: list[SearchResult] = []
        for m in (data.get("Page", {}) or {}).get("media", []) or []:
            t = m.get("title", {}) or {}
            # English title first — that's AniList's edge over romaji-only sources.
            display = t.get("english") or t.get("romaji") or t.get("native") or "Untitled"
            out.append(SearchResult(
                source=self.name,
                source_label=self.label,
                title=display,
                url=self.url_for(m.get("id")),
                author=_author_from_staff(m.get("staff", {})),
                cover_url=(m.get("coverImage", {}) or {}).get("large", "") or "",
                description=_clean_desc(m.get("description", ""))[:300],
                year=str((m.get("startDate", {}) or {}).get("year") or ""),
            ))
        return out

    # -- fetch (metadata from AniList, chapters from MangaDex) ---------------
    def fetch_series(self, url: str) -> SeriesMeta:
        m = _URL_RE.search(url or "")
        if not m:
            raise ValueError("Not an AniList manga URL (expected anilist.co/manga/<id>).")
        aid = int(m.group(1))
        data = _post(_DETAIL_QUERY, {"id": aid})
        media = data.get("Media", {}) or {}
        t = media.get("title", {}) or {}
        title = t.get("english") or t.get("romaji") or t.get("native") or "Untitled"

        # AniList holds no pages. Borrow MangaDex's readable chapter feed by matching
        # the title, so an AniList pick is still readable. If nothing matches, the
        # series imports as metadata-only (empty chapter list) rather than failing.
        chapters: list[ChapterMeta] = []
        source_for_pages = self.name
        external_id = str(aid)
        try:
            from .mangadex import MangaDexSource
            md = MangaDexSource()
            # Try the romaji title first (matches MangaDex's display), then English.
            for cand in (t.get("romaji"), t.get("english"), t.get("native")):
                if not cand:
                    continue
                hits = md.search(cand, limit=1)
                if hits:
                    md_meta = md.fetch_series(hits[0].url)
                    if md_meta.chapters:
                        chapters = md_meta.chapters
                        # Read pages via MangaDex from here on: store the MangaDex id
                        # so fetch_pages routes correctly and re-sync works.
                        source_for_pages = md.name
                        external_id = md_meta.external_id
                        break
        except Exception:
            pass  # metadata-only import is an acceptable fallback

        return SeriesMeta(
            source=source_for_pages,
            external_id=external_id,
            title=title,
            author=_author_from_staff(media.get("staff", {})),
            genres=[g for g in (media.get("genres", []) or []) if g],
            description=_clean_desc(media.get("description", "")),
            cover_url=(media.get("coverImage", {}) or {}).get("large", "") or "",
            chapters=chapters,
        )

    def fetch_pages(self, chapter: ChapterMeta) -> list[str]:
        # AniList never supplies pages directly. If fetch_series matched a MangaDex
        # series, its SeriesMeta.source is "mangadex", so the app dispatches page
        # fetches to the MangaDex adapter — this method only runs for a metadata-only
        # import, which has no readable pages.
        return []
