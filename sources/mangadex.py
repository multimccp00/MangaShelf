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


_MAX_JSON_BYTES = 16 * 1024 * 1024   # a MangaDex JSON response over 16 MB is abnormal


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read(_MAX_JSON_BYTES + 1)   # cap — never buffer an unbounded body
    if len(raw) > _MAX_JSON_BYTES:
        raise ValueError("MangaDex response exceeds size cap")
    data = json.loads(raw.decode("utf-8", "replace"))
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
        import difflib
        def _norm(s: str) -> str:
            return "".join(ch for ch in s.lower() if ch.isalnum())
        nq = _norm(query)

        def _best_score(titles: list[str]) -> float:
            # Score against the BEST-matching of the manga's titles (main + all alt
            # titles, incl. the English one). This is why "my dress up darling" finds
            # "Sono Bisque Doll…" — it matches the English ALT title, not the display
            # (romaji) title, so ranking on the display title alone buried it.
            # An EXACT match on any title (score 1.0) must always beat a mere
            # substring/fuzzy match, so non-exact scores are capped below 1.0 —
            # otherwise a spinoff like "My Dress-Up Darling 107.5" (query is a
            # substring → bonus) would outrank the real "My Dress-Up Darling".
            best = 0.0
            for t in titles:
                nt = _norm(t)
                if not nt:
                    continue
                if nt == nq:
                    return 1.0                  # exact on some title variant — wins
                ratio = difflib.SequenceMatcher(None, nq, nt).ratio()
                if nq and nq in nt:
                    ratio = 0.5 + 0.4 * ratio   # substring: strong, but < exact 1.0
                best = max(best, ratio)
            return min(best, 0.99)              # only an exact match reaches 1.0

        variants = self._spelling_variants(query)
        # Query each variant in turn, de-duping by url. Short-circuit as soon as some
        # result has an EXACT match on ANY of its titles (common case decided on
        # variant #1); harder spacing cases fall through to the rest.
        seen: set[str] = set()
        gathered: list[tuple[SearchResult, list[str]]] = []
        for variant in variants:
            for r, titles in self._run_search(variant, limit):
                if r.url in seen:
                    continue
                seen.add(r.url)
                gathered.append((r, titles))
            if any(_best_score(t) >= 1.0 for _r, t in gathered):
                break

        gathered.sort(key=lambda pair: _best_score(pair[1]), reverse=True)
        # Carry each hit's alt-title-aware score so combined web search (search_all)
        # ranks it by how well the query matched ANY of its titles — not just the
        # romaji display title, which would bury an English-query match.
        out: list[SearchResult] = []
        for r, titles in gathered[:limit]:
            r.match_score = _best_score(titles)
            out.append(r)
        return out

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

    def _run_search(self, title: str, limit: int) -> list[tuple[SearchResult, list[str]]]:
        """Returns (result, all_titles) pairs. all_titles = the main title in every
        language + every alt title, so the caller can rank against the ENGLISH name
        even when the display title is romaji."""
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
        out: list[tuple[SearchResult, list[str]]] = []
        for data in j.get("data", []):
            mid = data.get("id")
            attr = data.get("attributes", {})
            display = _first_localized(attr.get("title", {})) or "Untitled"
            # Collect ALL titles for ranking: main (every lang) + all alt titles.
            all_titles: list[str] = [str(v) for v in (attr.get("title", {}) or {}).values() if v]
            for alt in (attr.get("altTitles", []) or []):
                all_titles.extend(str(v) for v in (alt or {}).values() if v)
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
            out.append((SearchResult(
                source=self.name, source_label=self.label, title=display,
                url=self.url_for(mid), author=author, cover_url=cover_url,
                description=desc[:300], year=year,
            ), all_titles))
        return out

    def url_for(self, external_id: str) -> str:
        return f"https://mangadex.org/title/{external_id}" if external_id else ""

    def chapter_count(self, url: str) -> int:
        """Readable chapter count, resolved cheaply for the search card (feed?limit=1
        returns just the total, no chapters downloaded). Prefers English; if the
        series has no English chapters, falls back to the total across ALL languages
        in a SINGLE call — deliberately avoiding the per-language probing that
        fetch_series uses, so annotating a whole result grid stays fast. -1 on error."""
        m = _UUID_RE.search(url or "")
        if not m:
            return -1
        mid = m.group(1)
        try:
            qs = urllib.parse.urlencode({"translatedLanguage[]": "en", "limit": 1})
            total = _get(f"{_API}/manga/{mid}/feed?{qs}").get("total", 0)
            if total:
                return int(total)
            # No English → total across every language, one request.
            return int(_get(f"{_API}/manga/{mid}/feed?limit=1").get("total", 0))
        except Exception:
            return -1

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
        """Readable chapters, ascending, de-duped per chapter number. Prefers
        English, but if the series has NO English chapters (many don't — they're
        translated to other languages) it FALLS BACK to whatever language actually
        has the most chapters, rather than showing an empty list."""
        chapters = self._feed_for_lang(mid, lang)
        if chapters:
            return chapters
        # No English → find which languages this series is actually available in,
        # and use the one with the most chapters.
        best_lang = self._most_available_language(mid, exclude=lang)
        if best_lang:
            return self._feed_for_lang(mid, best_lang)
        return []

    def _feed_for_lang(self, mid: str, lang: str) -> list[ChapterMeta]:
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

    def _most_available_language(self, mid: str, exclude: str = "en") -> str:
        """The translated language (other than `exclude`) with the most chapters for
        this manga, from its availableTranslatedLanguages, tie-broken by a quick
        per-language count. Returns '' if none."""
        try:
            j = _get(f"{_API}/manga/{mid}")
            langs = j.get("data", {}).get("attributes", {}).get("availableTranslatedLanguages", []) or []
        except Exception:
            langs = []
        langs = [L for L in langs if L and L != exclude]
        if not langs:
            return ""
        # Count chapters per candidate (limit=1 returns the total cheaply).
        best, best_n = "", -1
        for L in langs:
            try:
                qs = urllib.parse.urlencode({"translatedLanguage[]": L, "limit": 1})
                total = _get(f"{_API}/manga/{mid}/feed?{qs}").get("total", 0)
            except Exception:
                total = 0
            if total > best_n:
                best, best_n = L, total
        return best

    def fetch_pages(self, chapter: ChapterMeta) -> list[str]:
        j = _get(f"{_API}/at-home/server/{chapter.id}")
        base = j["baseUrl"]
        h = j["chapter"]["hash"]
        files = j["chapter"]["data"]  # full-quality page filenames
        return [f"{base}/data/{h}/{fn}" for fn in files]
