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

from .base import ChapterMeta, MangaSource, SeriesMeta

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

    def matches(self, url: str) -> bool:
        return "mangadex.org" in url and bool(_UUID_RE.search(url))

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
