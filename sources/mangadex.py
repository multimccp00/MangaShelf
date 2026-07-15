"""MangaDex import adapter.

Uses MangaDex's official public JSON API (https://api.mangadex.org) — no HTML
scraping — so it's stable and sanctioned. This is the reference adapter that ships
with the app.

Series URL form:  https://mangadex.org/title/<uuid>/<slug>
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request

from .base import ChapterMeta, MangaSource, SearchResult, SeriesMeta

_API = "https://api.mangadex.org"
_UPLOADS = "https://uploads.mangadex.org"
_UUID_RE = re.compile(r"/title/([0-9a-fA-F-]{36})")
_UA = "MangaShelf/1.0 (+https://github.com/; self-hosted personal library)"
_RATE = 0.25  # polite pause between API calls (MangaDex allows ~5 req/s)


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    time.sleep(_RATE)
    return data


def _first_localized(d: dict, prefer: str = "en") -> str:
    """MangaDex localizes many fields as {lang: text}. Prefer English, else any."""
    if not isinstance(d, dict) or not d:
        return ""
    return d.get(prefer) or next(iter(d.values()), "")


class MangaDexSource(MangaSource):
    name = "mangadex"
    label = "MangaDex"
    example = "https://mangadex.org/title/<id>"
    can_search = True

    def matches(self, url: str) -> bool:
        return "mangadex.org" in url and bool(_UUID_RE.search(url))

    def search(self, query: str, limit: int = 20) -> list[SearchResult]:
        """Title search via MangaDex's official /manga endpoint. Returns lightweight
        results (title/author/cover/url); the chapter list is only fetched later when
        the user picks one to import (via the normal fetch_series flow).

        MangaDex matches titles fairly literally, so a spacing difference misses (e.g.
        "high school of the dead" won't find "Highschool of the Dead"). We query the
        original plus common spelling variants and merge, de-duped by manga id, so the
        obvious match surfaces regardless of how the user typed it."""
        query = (query or "").strip()
        if not query:
            return []
        variants = self._spelling_variants(query)
        # Gather from EVERY variant (so a spacing-fixed variant that finds the exact
        # match isn't crowded out by the first variant's fuzzy hits), de-dupe by id,
        # then rank by closeness to the query so the obvious match floats to the top.
        seen: set[str] = set()
        gathered: list[SearchResult] = []
        for variant in variants:
            for r in self._run_search(variant, limit):
                if r.url in seen:
                    continue
                seen.add(r.url)
                gathered.append(r)

        import difflib
        def _norm(s: str) -> str:
            return "".join(ch for ch in s.lower() if ch.isalnum())
        nq = _norm(query)
        def _score(r: SearchResult) -> float:
            nt = _norm(r.title)
            if nt == nq:
                return 1.0                      # exact (ignoring spaces/case)
            ratio = difflib.SequenceMatcher(None, nq, nt).ratio()
            if nq and nq in nt:
                ratio += 0.3                    # query is a substring of the title
            return ratio
        gathered.sort(key=_score, reverse=True)
        return gathered[:limit]

    @staticmethod
    def _spelling_variants(query: str) -> list[str]:
        """The original query first, then near-variants that catch spacing/joining
        differences MangaDex is picky about. Order = search priority."""
        q = query.strip()
        variants = [q]
        collapsed = q.replace(" ", "")
        if collapsed != q:
            variants.append(collapsed)
        # Join just the first two words (handles "high school" -> "highschool").
        parts = q.split()
        if len(parts) >= 2:
            joined_first = parts[0] + parts[1] + ("" if len(parts) == 2 else " " + " ".join(parts[2:]))
            if joined_first not in variants:
                variants.append(joined_first)
        return variants

    def _run_search(self, title: str, limit: int) -> list[SearchResult]:
        qs = urllib.parse.urlencode(
            [("title", title), ("limit", min(int(limit), 50)),
             ("includes[]", "author"), ("includes[]", "cover_art"),
             ("order[relevance]", "desc"),
             ("contentRating[]", "safe"), ("contentRating[]", "suggestive"),
             ("contentRating[]", "erotica")],
        )
        try:
            j = _get(f"{_API}/manga?{qs}")
        except Exception:
            return []
        out: list[SearchResult] = []
        for data in j.get("data", []):
            mid = data.get("id")
            attr = data.get("attributes", {})
            title = _first_localized(attr.get("title", {})) or "Untitled"
            year = str(attr.get("year") or "")
            desc = _first_localized(attr.get("description", {}))
            author = ""
            cover_file = ""
            for rel in data.get("relationships", []):
                ra = rel.get("attributes", {}) or {}
                if rel.get("type") == "author" and not author:
                    author = ra.get("name", "") or author
                elif rel.get("type") == "cover_art":
                    cover_file = ra.get("fileName", "") or cover_file
            # 256px thumbnail keeps the search grid light.
            cover_url = f"{_UPLOADS}/covers/{mid}/{cover_file}.256.jpg" if cover_file else ""
            out.append(SearchResult(
                source=self.name, source_label=self.label, title=title,
                url=self.url_for(mid), author=author, cover_url=cover_url,
                description=desc[:300], year=year,
            ))
        return out

    def url_for(self, external_id: str) -> str:
        return f"https://mangadex.org/title/{external_id}" if external_id else ""

    def fetch_series(self, url: str) -> SeriesMeta:
        m = _UUID_RE.search(url)
        if not m:
            raise ValueError("Not a MangaDex title URL (expected /title/<uuid>).")
        mid = m.group(1)

        j = _get(f"{_API}/manga/{mid}?includes[]=author&includes[]=artist&includes[]=cover_art")
        data = j["data"]
        attr = data["attributes"]

        title = _first_localized(attr.get("title", {})) or "Untitled"
        description = _first_localized(attr.get("description", {}))
        genres = [
            _first_localized(t["attributes"]["name"])
            for t in attr.get("tags", [])
            if t.get("attributes", {}).get("group") in ("genre", "theme")
        ]
        genres = [g for g in genres if g]

        author = ""
        cover_file = ""
        for rel in data.get("relationships", []):
            rtype = rel.get("type")
            ra = rel.get("attributes", {}) or {}
            if rtype in ("author", "artist") and not author:
                author = ra.get("name", "") or author
            elif rtype == "cover_art":
                cover_file = ra.get("fileName", "") or cover_file

        cover_url = f"{_UPLOADS}/covers/{mid}/{cover_file}" if cover_file else ""
        chapters = self._fetch_feed(mid)

        return SeriesMeta(
            source=self.name,
            external_id=mid,
            title=title,
            author=author,
            genres=genres,
            description=description,
            cover_url=cover_url,
            chapters=chapters,
        )

    def _fetch_feed(self, mid: str, lang: str = "en") -> list[ChapterMeta]:
        """All readable chapters in one language, ascending. De-dupes the common
        case of multiple scanlation groups uploading the same chapter number."""
        out: list[ChapterMeta] = []
        offset = 0
        while True:
            qs = urllib.parse.urlencode(
                {
                    "translatedLanguage[]": lang,
                    "order[volume]": "asc",
                    "order[chapter]": "asc",
                    "limit": 100,
                    "offset": offset,
                },
            )
            j = _get(f"{_API}/manga/{mid}/feed?{qs}")
            for c in j.get("data", []):
                a = c.get("attributes", {})
                if a.get("externalUrl"):
                    continue  # chapter is hosted off-site; no pages to fetch
                out.append(ChapterMeta(
                    id=c["id"],
                    number=a.get("chapter") or "",
                    title=a.get("title") or "",
                    language=a.get("translatedLanguage") or lang,
                ))
            offset += 100
            if offset >= j.get("total", 0):
                break

        seen: set[str] = set()
        uniq: list[ChapterMeta] = []
        for c in out:
            key = c.number or c.id
            if key in seen:
                continue
            seen.add(key)
            uniq.append(c)
        return uniq

    def fetch_pages(self, chapter: ChapterMeta) -> list[str]:
        j = _get(f"{_API}/at-home/server/{chapter.id}")
        base = j["baseUrl"]
        h = j["chapter"]["hash"]
        files = j["chapter"]["data"]  # full-quality page filenames
        return [f"{base}/data/{h}/{fn}" for fn in files]
