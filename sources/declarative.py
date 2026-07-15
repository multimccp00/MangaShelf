"""Declarative "Source" engine — runs user-supplied JSON manifests safely.

A user extends MangaShelf with support for a new site by installing a **manifest**
(a JSON file describing how to read that site with regex patterns). This engine
interprets the manifest — it NEVER executes user-supplied code. That's the whole
point: in a multi-user app, an extension that could run Python would be remote code
execution. A manifest can only produce data (or fail); it can't touch the host.

Design + schema: see EXTENSIONS_DESIGN.md. v1 supports HTML sites (regex on the
fetched/rendered page). JSON-API sites are a later tier.

Safety properties (all enforced here):
  - every fetch is SSRF-guarded (reuses ai._is_public_url) — no localhost/LAN/metadata
  - every read is size-capped — no unbounded response into memory
  - regexes run under a match budget — catastrophic backtracking can't hang a worker
  - no eval / no import of manifest content — patterns are data, interpreted only
"""
from __future__ import annotations

import re
import urllib.request
from urllib.parse import urljoin, urlparse

from .base import ChapterMeta, MangaSource, SeriesMeta

# Reuse the AI adapter's already-audited primitives so safety stays in one place:
# the SSRF guard, the shared browser render, the UA, and the numbered-page helpers.
from .ai import _is_public_url, _render, _UA, _NUMBERED_RE, _page_num

MANIFEST_VERSION = 1
_MAX_HTML_BYTES = 8 * 1024 * 1024      # a manga index/chapter page over 8 MB is abnormal
_HTTP_TIMEOUT = 45


# --------------------------------------------------------------- validation --
class ManifestError(ValueError):
    """A manifest is malformed or unsupported. Raised during validation so the
    loader can skip it with a clear reason instead of crashing discovery."""


def _need(d: dict, key: str, typ, where: str):
    if key not in d:
        raise ManifestError(f"{where}: missing required field '{key}'")
    if not isinstance(d[key], typ):
        raise ManifestError(f"{where}: '{key}' must be {typ.__name__}")
    return d[key]


def _compile(pattern: str, where: str) -> "re.Pattern":
    if not isinstance(pattern, str) or not pattern:
        raise ManifestError(f"{where}: expected a non-empty regex string")
    try:
        return re.compile(pattern, re.I | re.S)
    except re.error as exc:
        raise ManifestError(f"{where}: invalid regex ({exc})")


def validate_manifest(m: dict) -> dict:
    """Validate + normalize a manifest dict. Raises ManifestError on any problem.
    Returns the manifest unchanged (regexes are compiled lazily in the engine so
    validation stays cheap and side-effect free)."""
    if not isinstance(m, dict):
        raise ManifestError("manifest must be a JSON object")
    ver = m.get("manifest_version")
    if ver != MANIFEST_VERSION:
        raise ManifestError(f"unsupported manifest_version {ver!r} (expected {MANIFEST_VERSION})")
    for f in ("id", "name", "version", "type"):
        _need(m, f, str, "manifest")
    if m["type"] not in ("html", "json"):
        raise ManifestError(f"unsupported type {m['type']!r} (v1 supports 'html', 'json')")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", m["id"]):
        raise ManifestError("id must be lowercase alphanumeric / -_ (max 64 chars)")

    match = _need(m, "match", dict, "manifest")
    _need(match, "host", str, "match")

    series = _need(m, "series", dict, "manifest")
    if m["type"] == "html":
        # These regex fields are required to produce a title + chapter list.
        _compile(_need(series, "title_regex", str, "series"), "series.title_regex")
        _compile(_need(series, "chapter_link_regex", str, "series"), "series.chapter_link_regex")
        if "chapter_number_regex" in series:
            _compile(series["chapter_number_regex"], "series.chapter_number_regex")
        for opt in ("author_regex", "cover_regex"):
            if opt in series:
                _compile(series[opt], f"series.{opt}")
        if "genres_regex" in series:
            _compile(series["genres_regex"], "series.genres_regex")

        pages = _need(m, "pages", dict, "manifest")
        # Pages are selected by CDN host + numbered-filename (robust across chapters,
        # exactly like the AI heuristic) and/or an explicit image_url_regex.
        if "image_host" not in pages and "image_url_regex" not in pages:
            raise ManifestError("pages: need 'image_host' or 'image_url_regex'")
        if "image_url_regex" in pages:
            _compile(pages["image_url_regex"], "pages.image_url_regex")
    else:  # json
        # JSON-API sites: endpoints + safe JSONPath-subset extractors.
        _need(series, "endpoint", str, "series")
        _need(series, "title", str, "series")
        _need(series, "chapters_endpoint", str, "series")
        _need(series, "chapter_id", str, "series")
        pages = _need(m, "pages", dict, "manifest")
        _need(pages, "endpoint", str, "pages")
        _need(pages, "image_template", str, "pages")
        # Endpoints are URL templates with {id}/{chapter_id} placeholders — reject
        # any that aren't absolute https so a manifest can't smuggle a weird scheme.
        for ep in (series["endpoint"], series["chapters_endpoint"], pages["endpoint"]):
            if not ep.startswith(("http://", "https://")):
                raise ManifestError(f"json endpoint must be an absolute URL: {ep!r}")

    # Optional "search" block — lets this source be searched by title (joins the
    # web-search feature). Scrapes the site's search-results page with regex.
    if "search" in m:
        sb = m["search"]
        if not isinstance(sb, dict):
            raise ManifestError("search must be an object")
        # {query} is replaced with the URL-encoded query.
        url_tmpl = _need(sb, "url", str, "search")
        if "{query}" not in url_tmpl:
            raise ManifestError("search.url must contain the {query} placeholder")
        if not url_tmpl.startswith(("http://", "https://")):
            raise ManifestError("search.url must be an absolute URL")
        # A regex that finds each result's series-page link (+ optional title).
        _compile(_need(sb, "result_regex", str, "search"), "search.result_regex")
        for opt in ("title_regex", "cover_regex"):
            if opt in sb:
                _compile(sb[opt], f"search.{opt}")
    return m


# ------------------------------------------------------------------ engine --
def _fetch_html(url: str) -> str:
    """Plain HTTP GET of a page, SSRF-guarded + size-capped. For non-JS sites."""
    if not _is_public_url(url):
        raise ValueError("refusing to fetch a non-public address")
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
        raw = r.read(_MAX_HTML_BYTES + 1)
    if len(raw) > _MAX_HTML_BYTES:
        raise ValueError("page exceeds size cap")
    return raw.decode("utf-8", "replace")


def _fetch_json(url: str) -> object:
    """GET + parse JSON, SSRF-guarded + size-capped."""
    import json as _json
    if not _is_public_url(url):
        raise ValueError("refusing to fetch a non-public address")
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
        raw = r.read(_MAX_HTML_BYTES + 1)
    if len(raw) > _MAX_HTML_BYTES:
        raise ValueError("response exceeds size cap")
    return _json.loads(raw.decode("utf-8", "replace"))


def _jpath(data: object, expr: str) -> object:
    """Resolve a SAFE, tiny subset of JSONPath: `$.a.b`, `$.a[0].b`, `$.a[*].b`.
    Returns a scalar, or a LIST when `[*]` is used. No eval, no filters — data only.
    Unknown/missing keys resolve to '' (scalar) or [] (wildcard) rather than raising."""
    if not isinstance(expr, str) or not expr.startswith("$"):
        return expr  # a literal (not a path)
    # Tokenize: .key  or  [0]  or  [*]
    tokens = re.findall(r"\.([A-Za-z0-9_\-]+)|\[(\d+)\]|\[(\*)\]", expr)
    cur = [data]        # work on a list to support [*] fan-out
    fanned = False
    for key, idx, star in tokens:
        nxt = []
        for node in cur:
            if key:
                if isinstance(node, dict):
                    nxt.append(node.get(key, ""))
                else:
                    nxt.append("")
            elif idx:
                i = int(idx)
                nxt.append(node[i] if isinstance(node, list) and 0 <= i < len(node) else "")
            elif star:
                fanned = True
                if isinstance(node, list):
                    nxt.extend(node)
        cur = nxt
    return cur if fanned else (cur[0] if cur else "")


def _interp(template: str, ctx: dict, root: object) -> str:
    """Fill a URL/string template. {name} → ctx[name]; {$.a.b} → JSONPath on root."""
    def repl(mo):
        inner = mo.group(1)
        if inner.startswith("$"):
            v = _jpath(root, inner)
            return str(v[0] if isinstance(v, list) and v else v)
        return str(ctx.get(inner, ""))
    return re.sub(r"\{([^}]+)\}", repl, template)


def _findall_budgeted(pattern: "re.Pattern", text: str, limit: int = 5000) -> list:
    """re.findall with a hard result cap so a pathological pattern can't blow up
    memory. (Python's re has no timeout; the cap + the size-limited input bound it.)"""
    out = []
    for i, mm in enumerate(pattern.finditer(text)):
        if i >= limit:
            break
        out.append(mm)
    return out


class DeclarativeSource(MangaSource):
    """A Source driven entirely by a validated manifest. One instance per manifest."""

    def __init__(self, manifest: dict) -> None:
        self.m = validate_manifest(manifest)
        self.name = self.m["id"]
        self.label = self.m.get("name", self.m["id"])
        self.example = self.m.get("example_url", "")
        self._match = self.m["match"]
        self._series = self.m["series"]
        self._pages = self.m["pages"]
        self._type = self.m["type"]
        self._needs_browser = bool(self.m.get("needs_browser", False))
        self._referer_mode = (self.m.get("headers", {}) or {}).get("referer", "")
        self._search = self.m.get("search")
        self.can_search = self._search is not None

    # --- title search (optional; only if the manifest has a "search" block) ---
    def search(self, query: str, limit: int = 20):
        from .base import SearchResult
        sb = self._search
        if not sb or not (query or "").strip():
            return []
        import urllib.parse as _up
        url = sb["url"].replace("{query}", _up.quote(query.strip()))
        try:
            if self._needs_browser:
                html = _render(url).get("html", "") or self._render_html(url)
            else:
                html = _fetch_html(url)
        except Exception:
            return []
        result_rx = re.compile(sb["result_regex"], re.I | re.S)
        title_rx = re.compile(sb["title_regex"], re.I | re.S) if sb.get("title_regex") else None
        cover_rx = re.compile(sb["cover_regex"], re.I | re.S) if sb.get("cover_regex") else None
        out = []
        seen = set()
        for mm in _findall_budgeted(result_rx, html):
            href = mm.group(1) if mm.groups() else mm.group(0)
            full = urljoin(url, href.strip())
            if full in seen:
                continue
            seen.add(full)
            # Title: from a capture group 2 on the result regex, else a nearby title
            # regex applied to the matched chunk, else the URL slug.
            title = ""
            if mm.groups() and len(mm.groups()) >= 2 and mm.group(2):
                title = re.sub(r"<[^>]+>", "", mm.group(2)).strip()
            elif title_rx:
                tm = title_rx.search(mm.group(0))
                if tm and tm.groups():
                    title = re.sub(r"<[^>]+>", "", tm.group(1)).strip()
            if not title:
                title = (urlparse(full).path.rstrip("/").split("/")[-1] or full).replace("-", " ").title()
            cover = ""
            if cover_rx:
                cm = cover_rx.search(mm.group(0))
                if cm and cm.groups():
                    cover = urljoin(url, cm.group(1).strip())
            out.append(SearchResult(
                source=self.name, source_label=self.label, title=title,
                url=full, cover_url=cover,
            ))
            if len(out) >= limit:
                break
        return out

    def _render_html(self, url: str) -> str:
        # Fallback: some rendered pages return links but not raw html; join a minimal
        # HTML from the rendered link list so the search regex still has something.
        r = _render(url)
        return "\n".join(f'<a href="{h}">{t}</a>' for h, t in r.get("links", []))

    # --- routing ---
    def matches(self, url: str) -> bool:
        if not url.startswith("http") or not _is_public_url(url):
            return False
        host = urlparse(url).netloc.lower()
        if self._match["host"].lower() not in host:
            return False
        path = urlparse(url).path
        if "path_contains" in self._match and self._match["path_contains"] not in url:
            return False
        if "path_regex" in self._match and not re.search(self._match["path_regex"], path):
            return False
        return True

    # --- HTML retrieval (plain HTTP, or Playwright when the site needs JS) ---
    def _get_page(self, url: str, scroll: bool = False) -> dict:
        """Return {"html": str, "imgs": [str], "links": [(href,text)]}. Plain HTTP
        yields html only; browser render also yields resolved imgs + links."""
        if self._needs_browser:
            r = _render(url, scroll=scroll)   # SSRF + size handled inside ai._render's browser
            return {"html": "", "imgs": r["imgs"],
                    "links": [(h, t) for h, t in r["links"] if h],
                    "title": r.get("title", ""), "h1": r.get("h1", "")}
        html = _fetch_html(url)
        return {"html": html, "imgs": [], "links": [], "title": "", "h1": ""}

    def _first(self, pattern_key: str, html: str) -> str:
        pat = self._series.get(pattern_key)
        if not pat:
            return ""
        m = re.compile(pat, re.I | re.S).search(html)
        return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m and m.groups() else ""

    def _match_id(self, url: str) -> str:
        """The site's own id for a URL, from match.path_regex group 1 (json sites),
        else the path — used to fill {id} in json endpoints."""
        pr = self._match.get("path_regex")
        if pr:
            mm = re.search(pr, urlparse(url).path)
            if mm and mm.groups():
                return mm.group(1)
        return urlparse(url).path.strip("/")

    def fetch_series(self, url: str) -> SeriesMeta:
        if not self.matches(url):
            raise ValueError("This URL doesn't match this source.")
        if self._type == "json":
            return self._fetch_series_json(url)
        return self._fetch_series_html(url)

    def _fetch_series_json(self, url: str) -> SeriesMeta:
        sid = self._match_id(url)
        ctx = {"id": sid}
        root = _fetch_json(_interp(self._series["endpoint"], ctx, None))
        title = str(_jpath(root, self._series["title"]) or "Untitled")
        author = str(_jpath(root, self._series["author"]) or "") if "author" in self._series else ""
        cover = _interp(self._series["cover"], ctx, root) if "cover" in self._series else ""
        # Chapter list: a second endpoint yields parallel id/number lists.
        feed = _fetch_json(_interp(self._series["chapters_endpoint"], ctx, None))
        ids = _jpath(feed, self._series["chapter_id"])
        nums = _jpath(feed, self._series["chapter_number"]) if "chapter_number" in self._series else []
        ids = ids if isinstance(ids, list) else [ids]
        nums = nums if isinstance(nums, list) else [nums]
        chapters = []
        for i, cid in enumerate(ids):
            num = str(nums[i]) if i < len(nums) and nums[i] not in ("", None) else ""
            chapters.append(ChapterMeta(id=str(cid), number=num, url=""))
        if not chapters:
            raise ValueError("No chapters returned by this source's JSON feed.")
        domain = urlparse(url).netloc
        return SeriesMeta(source=self.name, external_id=f"{self.name}:{sid}",
                          title=title, author=author, cover_url=str(cover), chapters=chapters)

    def _fetch_series_html(self, url: str) -> SeriesMeta:
        page = self._get_page(url)
        html = page["html"]

        title = self._first("title_regex", html) or page.get("h1") or page.get("title") or "Untitled"
        author = self._first("author_regex", html)
        cover = ""
        if "cover_regex" in self._series:
            m = re.compile(self._series["cover_regex"], re.I | re.S).search(html)
            if m and m.groups():
                cover = urljoin(url, m.group(1).strip())
        genres = []
        if "genres_regex" in self._series:
            gpat = re.compile(self._series["genres_regex"], re.I | re.S)
            genres = [re.sub(r"<[^>]+>", "", g.group(1)).strip()
                      for g in _findall_budgeted(gpat, html) if g.groups()]

        # Chapter links: from HTML (plain) or from the rendered link list (browser).
        crx = re.compile(self._series["chapter_link_regex"], re.I | re.S)
        num_rx = re.compile(self._series["chapter_number_regex"], re.I) if self._series.get("chapter_number_regex") else None
        hrefs: list[str] = []
        if self._needs_browser:
            for h, _t in page["links"]:
                if crx.search(h):
                    hrefs.append(h)
        else:
            for mm in _findall_budgeted(crx, html):
                hrefs.append(mm.group(1) if mm.groups() else mm.group(0))

        seen, chapters = set(), []
        for href in hrefs:
            full = urljoin(url, href)
            if full in seen:
                continue
            seen.add(full)
            num = ""
            if num_rx:
                nm = num_rx.search(href)
                if nm and nm.groups():
                    num = nm.group(1)
            chapters.append((float(num) if num.replace(".", "", 1).isdigit() else 0.0, full, num))
        chapters.sort(key=lambda c: c[0])
        chapter_metas = [ChapterMeta(id=full, number=num, url=full) for _n, full, num in chapters]
        if not chapter_metas:
            raise ValueError("No chapters matched this source's chapter pattern on the page.")

        domain = urlparse(url).netloc
        return SeriesMeta(source=self.name, external_id=f"{domain}{urlparse(url).path}",
                          title=title, author=author, genres=genres,
                          cover_url=cover, chapters=chapter_metas)

    def fetch_pages(self, chapter: ChapterMeta) -> list[str]:
        if self._type == "json":
            return self._fetch_pages_json(chapter)
        return self._fetch_pages_html(chapter)

    def _fetch_pages_json(self, chapter: ChapterMeta) -> list[str]:
        root = _fetch_json(_interp(self._pages["endpoint"], {"chapter_id": chapter.id}, None))
        # image_template like "{$.baseUrl}/data/{$.hash}/{$.files[*]}" — the [*]
        # element fans out into one URL per file.
        tmpl = self._pages["image_template"]
        # Find the fan-out path (the one with [*]) and iterate it.
        fan = re.search(r"\{(\$[^}]*\[\*\][^}]*)\}", tmpl)
        if not fan:
            one = _interp(tmpl, {}, root)
            return [one] if one else []
        files = _jpath(root, fan.group(1))
        files = files if isinstance(files, list) else [files]
        out = []
        for f in files:
            # Replace the fan-out placeholder with this file, resolve the rest.
            per = tmpl.replace("{" + fan.group(1) + "}", str(f))
            out.append(_interp(per, {}, root))
        return [u for u in out if u]

    def _fetch_pages_html(self, chapter: ChapterMeta) -> list[str]:
        # Chapter image pages usually need JS/lazy-load; render with scroll when the
        # manifest asks for a browser, else fetch plain HTML and regex the URLs.
        host = self._pages.get("image_host")
        url_rx = re.compile(self._pages["image_url_regex"], re.I) if self._pages.get("image_url_regex") else None
        if self._needs_browser:
            imgs = _render(chapter.id, scroll=True)["imgs"]
        else:
            html = _fetch_html(chapter.id)
            # Grab src / data-src style URLs, then filter below.
            imgs = re.findall(r'https?:[^\s"\'<>]+\.(?:jpe?g|png|webp|gif)', html, re.I)

        sel = []
        for u in imgs:
            if host and urlparse(u).netloc != host:
                continue
            if url_rx and not url_rx.search(u):
                continue
            if not host and not url_rx and not _NUMBERED_RE.search(u):
                continue
            sel.append(urljoin(chapter.id, u))
        sel = list(dict.fromkeys(sel))
        return sorted(sel, key=_page_num)

    def image_headers(self) -> dict[str, str]:
        if self._referer_mode == "origin":
            # Referer = the site origin — the hotlink-protection fix used elsewhere.
            return {"Referer": f"https://{self._match['host']}/"}
        return {}

    def url_for(self, external_id: str) -> str:
        return "https://" + external_id if external_id and "://" not in external_id else external_id
