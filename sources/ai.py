"""AI-assisted generic import adapter (fallback for unknown sites).

When no dedicated adapter handles a URL, this one renders the page in a headless
browser (so client-side JS runs), then asks a **local** LLM — any OpenAI-compatible
endpoint, e.g. LM Studio or Ollama — to work out two things:

  1. a regex that matches the site's chapter links, and
  2. a regex that matches a chapter's page-image URLs.

Those rules are cached per domain (sources/_ai_cache/<domain>.json), so after the
first paste every import from that site is deterministic and needs no LLM. The AI
runs at *design time* only, never in the download loop.

Requires a local LLM to be running; otherwise this adapter simply doesn't match
(the import falls back to "no source recognizes this link").
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .base import ChapterMeta, MangaSource, SeriesMeta

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_CACHE_DIR = Path(__file__).parent / "_ai_cache"

# OpenAI-compatible endpoints to probe, in order (LM Studio, then Ollama).
_LLM_ENDPOINTS = ["http://localhost:1234/v1", "http://localhost:11434/v1"]
_llm_base: str | None = None
_llm_model: str | None = None
_llm_checked_at = 0.0            # monotonic time of the last probe (0 = never)
_LLM_RECHECK_SECS = 30.0        # re-probe at most this often, so an LLM that
                                # starts AFTER the server becomes usable without
                                # a restart (was a permanent one-shot flag before).


# --- SSRF guard --------------------------------------------------------------
def _is_public_url(url: str) -> bool:
    """True only if `url` resolves to a public (non-private/loopback/link-local)
    address. The AI adapter fetches/renders WHATEVER URL it's given, so this stops
    it from being pointed at internal services (cloud metadata, localhost, LAN)."""
    try:
        host = urlparse(url).hostname
        if not host:
            return False
        # Resolve every address the host maps to; reject if ANY is non-public.
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return False
        return bool(infos)
    except Exception:
        return False


# --- local LLM (OpenAI-compatible) ------------------------------------------
def _discover_llm() -> tuple[str | None, str | None]:
    global _llm_base, _llm_model, _llm_checked_at
    now = time.monotonic()
    # Cached result still fresh, or we already found one → reuse it.
    if _llm_base and (now - _llm_checked_at) < _LLM_RECHECK_SECS:
        return _llm_base, _llm_model
    if _llm_base is None and _llm_checked_at and (now - _llm_checked_at) < _LLM_RECHECK_SECS:
        return None, None
    _llm_checked_at = now
    for base in _LLM_ENDPOINTS:
        try:
            req = urllib.request.Request(base + "/models")
            with urllib.request.urlopen(req, timeout=2) as r:
                data = json.loads(r.read())
            models = [m["id"] for m in data.get("data", [])]
            # Prefer a coder/instruct chat model over embedding models.
            chat = [m for m in models if "embed" not in m.lower()]
            if chat:
                _llm_base, _llm_model = base, chat[0]
                return _llm_base, _llm_model
        except Exception:
            continue
    _llm_base = _llm_model = None
    return None, None


def llm_available() -> bool:
    return _discover_llm()[0] is not None


def _llm_json(system: str, user: str, max_tokens: int = 800) -> dict:
    """Ask the LLM and parse a JSON object out of its reply."""
    base, model = _discover_llm()
    if not base:
        raise RuntimeError("No local LLM endpoint is available.")
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(base + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read(32 * 1024 * 1024 + 1)   # cap: an LLM reply over 32 MB is broken
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("LLM response too large")
    out = json.loads(raw)
    text = out["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", text, re.S)   # first JSON object in the reply
    if not m:
        raise ValueError(f"LLM did not return JSON: {text[:200]}")
    return json.loads(m.group(0))


# --- headless browser (shared, lazy) ----------------------------------------
_PW_LOCK = threading.Lock()
_PW = {"pw": None, "browser": None}


def _browser():
    if _PW["browser"] is None:
        from playwright.sync_api import sync_playwright
        _PW["pw"] = sync_playwright().start()
        _PW["browser"] = _PW["pw"].chromium.launch()
    return _PW["browser"]


def _shutdown_browser() -> None:
    """Close the shared Chromium + Playwright on process exit so it isn't orphaned."""
    with _PW_LOCK:
        try:
            if _PW["browser"] is not None:
                _PW["browser"].close()
        except Exception:
            pass
        try:
            if _PW["pw"] is not None:
                _PW["pw"].stop()
        except Exception:
            pass
        _PW["browser"] = _PW["pw"] = None


import atexit  # noqa: E402
atexit.register(_shutdown_browser)


def _render(url: str, scroll: bool = False) -> dict:
    """Load a URL in a browser (JS runs) and return its links + image URLs.
    `scroll=True` scrolls the page to trigger lazy-loaded chapter images and also
    reads common lazy-src attributes."""
    br = _browser()
    ctx = br.new_context(user_agent=_UA)
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2000)
        if scroll:
            # Step down the page so lazy-loaders fire, then back to top.
            for _ in range(24):
                page.mouse.wheel(0, 2400)
                page.wait_for_timeout(250)
            page.wait_for_timeout(800)
        links = page.eval_on_selector_all(
            "a[href]", "els => els.slice(0,400).map(e => [e.getAttribute('href'), (e.textContent||'').trim().slice(0,60)])")
        # Read src plus common lazy attributes so nothing is missed.
        imgs = page.eval_on_selector_all(
            "img",
            "els => els.flatMap(e => [e.currentSrc, e.getAttribute('src'), "
            "e.getAttribute('data-src'), e.getAttribute('data-original'), "
            "e.getAttribute('data-lazy-src')]).filter(Boolean)")
        title = page.title()
        h1 = page.eval_on_selector("h1", "e => e.textContent.trim()") if page.query_selector("h1") else ""
    finally:
        page.close()
        ctx.close()
    return {"links": links, "imgs": list(dict.fromkeys(imgs)), "title": title, "h1": h1}


# --- rule cache -------------------------------------------------------------
def _cache_path(domain: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-z0-9.-]", "_", domain.lower())
    return _CACHE_DIR / f"{safe}.json"


def _load_rules(domain: str) -> dict | None:
    p = _cache_path(domain)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _save_rules(domain: str, rules: dict) -> None:
    try:
        _cache_path(domain).write_text(json.dumps(rules, indent=2), encoding="utf-8")
    except Exception:
        pass


def _abs(base_url: str, href: str) -> str:
    return urljoin(base_url, href)


# Page images are numbered files (…/1.jpg, …/12.webp). This is the stable signal
# across chapters even when the middle of the path changes.
_NUMBERED_RE = re.compile(r"/(\d+)\.(?:jpe?g|png|webp|gif)(?:[?#]|$)", re.I)


def _dominant_image_host(imgs: list[str]) -> str | None:
    from collections import Counter
    hosts = [urlparse(u).netloc for u in imgs if _NUMBERED_RE.search(u)]
    if not hosts:
        return None
    host, n = Counter(hosts).most_common(1)[0]
    return host if n >= 3 else None   # a real run of pages, not a stray icon


def _page_num(u: str) -> int:
    m = _NUMBERED_RE.search(u)
    return int(m.group(1)) if m else 0


class GenericAISource(MangaSource):
    name = "ai"
    label = "AI (any site)"
    example = "any manga series page — an AI works out how to read it"

    def matches(self, url: str) -> bool:
        # Catch-all fallback: only when a local LLM is available AND the URL points
        # at a PUBLIC host (SSRF guard — never fetch internal/localhost/LAN targets).
        # Registered last, so dedicated adapters always win.
        return url.startswith("http") and llm_available() and _is_public_url(url)

    def image_headers(self) -> dict[str, str]:
        # Many manga CDNs 403 a hotlinked image without a Referer from the origin
        # site. The AI adapter targets arbitrary sites, so it's the one most likely
        # to need this. Referer is set per-series in fetch_series (cached on the
        # instance); default to empty if fetch_pages runs standalone.
        ref = getattr(self, "_referer", "")
        return {"Referer": ref} if ref else {}

    def fetch_series(self, url: str) -> SeriesMeta:
        domain = urlparse(url).netloc
        # Remember the origin so image_headers() can send a matching Referer when
        # the shared downloader fetches this site's pages.
        self._referer = f"{urlparse(url).scheme}://{domain}/"
        rules = _load_rules(domain) or {}
        rendered = _render(url)

        # Title is per-series — always from this page, never cached.
        title = rendered["h1"] or rendered["title"] or "Untitled"

        # 1) Chapter-link rule (domain-stable → cached across all series on the site).
        if "chapter_regex" not in rules:
            link_lines = "\n".join(f"{h} | {t}" for h, t in rendered["links"] if h)[:6000]
            ans = _llm_json(
                "You extract manga chapter lists from a web page. Reply with ONLY a JSON object.",
                f"Series page URL: {url}\nPage title: {rendered['title']}\n\n"
                f"Links on the page (href | text):\n{link_lines}\n\n"
                "Return JSON with:\n"
                '  "title": the series title (string),\n'
                '  "chapter_regex": a Python regex matching the chapter-page hrefs. It must '
                "work for ANY series on this site, so do NOT include this series' name or slug "
                "— capture only the structural pattern (e.g. a '/chapter/' path with a number). "
                "Exclude nav/login/social/other-series links.",
            )
            if ans.get("title"):
                title = ans["title"]   # the LLM's cleaner title for THIS series
            rules["chapter_regex"] = ans.get("chapter_regex", "")

        try:
            crx = re.compile(rules["chapter_regex"])
        except re.error:
            raise ValueError("The AI produced an invalid chapter pattern for this site.")

        seen, chapters = set(), []
        for href, text in rendered["links"]:
            if not href or not crx.search(href):
                continue
            full = _abs(url, href)
            if full in seen:
                continue
            seen.add(full)
            numm = re.search(r"(\d+(?:\.\d+)?)", text) or re.search(r"(\d+(?:\.\d+)?)", href)
            chapters.append((float(numm.group(1)) if numm else 0.0, full, text.strip()))
        # Chapter lists are usually newest-first on the page → sort ascending by number.
        chapters.sort(key=lambda c: c[0])
        chapter_metas = [ChapterMeta(id=full, number=(str(int(n)) if n == int(n) else str(n)) if n else "",
                                     title="", url=full) for n, full, text in chapters]
        if not chapter_metas:
            raise ValueError("The AI couldn't find any chapters on this page.")

        # 2) Image rule. Prefer a robust heuristic: page images are numbered files
        # (…/1.jpg, …/2.jpg) from one CDN host — that generalizes across chapters
        # even when the middle path varies. Fall back to the LLM for odd sites.
        if "image_host" not in rules and "image_regex" not in rules:
            sample = _render(chapter_metas[0].id, scroll=True)
            host = _dominant_image_host(sample["imgs"])
            if host:
                rules["image_host"] = host
            else:
                img_lines = "\n".join(sample["imgs"])[:6000]
                ans = _llm_json(
                    "You identify the real page images of an online manga chapter. Reply with ONLY JSON.",
                    f"Chapter page URL: {chapter_metas[0].id}\n\n"
                    f"Image URLs on the page:\n{img_lines}\n\n"
                    'Return JSON {"image_regex": <regex>} matching the manga page images. It '
                    "must work for EVERY chapter — match only the stable CDN host + filename "
                    "pattern; never hard-code chapter-specific ids/dates/uuids. Exclude logos, "
                    "ads, avatars, thumbnails, icons.",
                )
                rx = ans.get("image_regex", "")
                try:
                    re.compile(rx)
                except re.error:
                    raise ValueError("The AI produced an invalid image pattern for this site.")
                rules["image_regex"] = rx

        _save_rules(domain, rules)
        slug = re.sub(r"[^a-z0-9]+", "-", (urlparse(url).path or domain).lower()).strip("-") or domain
        return SeriesMeta(source=self.name, external_id=f"{domain}{urlparse(url).path}",
                          title=title, chapters=chapter_metas)

    def fetch_pages(self, chapter: ChapterMeta) -> list[str]:
        domain = urlparse(chapter.id).netloc
        rules = _load_rules(domain) or {}
        rendered = _render(chapter.id, scroll=True)
        imgs = rendered["imgs"]
        host = rules.get("image_host")
        if host:
            sel = [u for u in imgs if urlparse(u).netloc == host and _NUMBERED_RE.search(u)]
        elif rules.get("image_regex"):
            irx = re.compile(rules["image_regex"])
            sel = [u for u in imgs if irx.search(u)]
        else:
            return []
        sel = list(dict.fromkeys(_abs(chapter.id, u) for u in sel))
        return sorted(sel, key=_page_num)

    def url_for(self, external_id: str) -> str:
        # external_id is "<domain><path>"
        return "https://" + external_id if external_id and "://" not in external_id else external_id


def manifest_from_ai_rules(domain: str, name: str = "", author: str = "") -> dict | None:
    """Export the AI's cached rules for `domain` as a declarative Source manifest —
    a shareable extension that works WITHOUT an LLM. The AI cache
    ({chapter_regex, image_host|image_regex}) maps almost 1:1 onto the manifest
    schema; the only twist is that AI-scraped sites need JS rendering, so the
    manifest is html + needs_browser. Returns None if no rules are cached yet."""
    rules = _load_rules(domain)
    if not rules or not rules.get("chapter_regex"):
        return None
    ext_id = re.sub(r"[^a-z0-9]+", "-", domain.lower()).strip("-")[:64] or "site"
    pages: dict = {}
    if rules.get("image_host"):
        pages["image_host"] = rules["image_host"]
    if rules.get("image_regex"):
        pages["image_url_regex"] = rules["image_regex"]
    if not pages:
        return None
    return {
        "manifest_version": 1,
        "id": ext_id,
        "name": name or domain,
        "version": "1.0.0",
        "author": author or "AI-generated",
        "type": "html",
        "match": {"host": domain},
        "example_url": f"https://{domain}/",
        "needs_browser": True,   # AI sites are JS-rendered (that's why the AI was used)
        "series": {
            # The AI's chapter_regex matches hrefs in the rendered link list; the
            # declarative engine applies chapter_link_regex the same way when
            # needs_browser is true. Title comes from the page (h1/title) at runtime.
            "title_regex": r"<h1[^>]*>(.*?)</h1>",
            "chapter_link_regex": rules["chapter_regex"],
            "chapter_number_regex": r"(\d+(?:\.\d+)?)",
        },
        "pages": pages,
        "headers": {"referer": "origin"},
    }
