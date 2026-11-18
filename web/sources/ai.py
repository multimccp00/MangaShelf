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
import os
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
# Ollama answers /v1/models in ~2.0s once it holds a few models, so the old 2s
# budget lost a coin flip on every probe — the AI source appeared and vanished
# between requests, showing up as "No import source recognizes this link".
_LLM_PROBE_SECS = 8
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
def _pick_model(chat: list[str]) -> str:
    """Choose which loaded model to use. MANGASHELF_LLM_MODEL wins if it matches
    one (exact id, or a prefix so "qwen2.5-coder" finds "qwen2.5-coder:7b").
    Without it, prefer a coder/instruct model — this job is regex writing, and a
    vision model like llava happening to sort first produces garbage rules."""
    want = (os.environ.get("MANGASHELF_LLM_MODEL") or "").strip().lower()
    if want:
        for m in chat:
            if m.lower() == want:
                return m
        for m in chat:
            if m.lower().startswith(want):
                return m
        print(f"[sources] MANGASHELF_LLM_MODEL={want!r} not loaded; auto-picking instead")
    for kw in ("coder", "gemma3", "qwen2.5-abl", "code", "instruct"):
        for m in chat:
            if kw in m.lower():
                return m
    return chat[0]


def _discover_llm() -> tuple[str | None, str | None]:
    global _llm_base, _llm_model, _llm_checked_at
    now = time.monotonic()
    # Cached result still fresh, or we already found one → reuse it.
    if _llm_base and (now - _llm_checked_at) < _LLM_RECHECK_SECS:
        return _llm_base, _llm_model
    if _llm_base is None and _llm_checked_at and (now - _llm_checked_at) < _LLM_RECHECK_SECS:
        return None, None
    _llm_checked_at = now
    # Re-probe the endpoint that worked last time FIRST. Probing an endpoint with
    # nothing listening costs the full timeout twice ("localhost" resolves to both
    # ::1 and 127.0.0.1), so checking an absent LM Studio ahead of a live Ollama
    # would add that dead wait to a request that is about to succeed anyway.
    endpoints = _LLM_ENDPOINTS
    if _llm_base in endpoints:
        endpoints = [_llm_base] + [b for b in endpoints if b != _llm_base]
    for base in endpoints:
        try:
            req = urllib.request.Request(base + "/models")
            with urllib.request.urlopen(req, timeout=_LLM_PROBE_SECS) as r:
                data = json.loads(r.read())
            models = [m["id"] for m in data.get("data", [])]
            # Prefer a coder/instruct chat model over embedding models.
            chat = [m for m in models if "embed" not in m.lower()]
            if chat:
                _llm_base, _llm_model = base, _pick_model(chat)
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
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        # LLMs emit regexes like "\d+" with a bare backslash — invalid JSON escape.
        # Double any backslash not starting a valid escape, then retry.
        return json.loads(re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", m.group(0)))


# --- headless browser (per-thread, lazy) ------------------------------------
# Playwright's sync API is THREAD-AFFINE: a browser started on one thread cannot
# be driven from another — it raises "cannot switch to a different thread". The
# server renders from more than one thread (FastAPI's threadpool serves preview,
# a background worker runs the import job), so a single shared browser worked for
# whichever thread got there first and broke the import for the next one.
# Each thread therefore gets its own browser, reused for every page IT renders.
_PW_LOCK = threading.Lock()
_PW_LOCAL = threading.local()
_PW_ALL: list = []   # every (playwright, browser) started, for shutdown


def _browser():
    br = getattr(_PW_LOCAL, "browser", None)
    if br is None:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        br = pw.chromium.launch()
        _PW_LOCAL.pw, _PW_LOCAL.browser = pw, br
        with _PW_LOCK:
            _PW_ALL.append((pw, br))
    return br


def _shutdown_browser() -> None:
    """Close every thread's Chromium + Playwright on process exit.
    ponytail: atexit runs on the main thread, so closing another thread's browser
    hits the same thread affinity and is swallowed — those Chromium children are
    reaped when the process exits anyway. Give each worker its own explicit
    shutdown if orphaned browsers ever show up in practice."""
    with _PW_LOCK:
        entries = list(_PW_ALL)
        _PW_ALL.clear()
    for pw, br in entries:
        try:
            br.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass


import atexit  # noqa: E402
atexit.register(_shutdown_browser)


def _render(url: str, scroll: bool = False) -> dict:
    """Load a URL in a browser (JS runs) and return its links + image URLs.
    `scroll=True` scrolls the page to trigger lazy-loaded chapter images and also
    reads common lazy-src attributes."""
    br = _browser()
    ctx = br.new_context(user_agent=_UA)
    page = ctx.new_page()
    collected: dict[str, None] = {}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2000)
        if scroll:
            # Step down the page so lazy-loaders fire, stopping as soon as no new
            # image URLs appear. The scroll used to be a flat 24 steps — ~6s spent
            # per chapter whether or not the site lazy-loads at all. Sites that put
            # every page URL in the DOM up front (most of them) now exit after two
            # rounds; genuinely lazy ones still get the full budget.
            img_js = ("Array.from(document.images).flatMap(e => [e.currentSrc, "
                      "e.getAttribute('src'), e.getAttribute('data-src'), "
                      "e.getAttribute('data-original'), e.getAttribute('data-lazy-src')])"
                      ".filter(Boolean)")
            # Some readers scroll an inner element (e.g. <main>), not the window, and
            # may unload off-screen images — so also scroll those containers and
            # accumulate URLs as we go instead of reading the DOM once at the end.
            inner_js = ("Array.from(document.querySelectorAll('*')).filter(e => "
                        "e.scrollHeight > e.clientHeight + 50 && "
                        "/auto|scroll/.test(getComputedStyle(e).overflowY))"
                        ".forEach(e => e.scrollBy(0, 2400))")
            page.mouse.move(400, 300)   # wheel events go to the element under the cursor
            stable, last = 0, -1
            for _ in range(120):
                collected.update(dict.fromkeys(page.evaluate(img_js)))
                found = len(collected)
                stable = stable + 1 if found == last else 0
                last = found
                if stable >= 3:
                    break
                page.evaluate(inner_js)
                page.mouse.wheel(0, 2400)
                page.wait_for_timeout(250)
            page.wait_for_timeout(400)
        # Cap high enough for a long-running series' full chapter list plus the
        # site's nav/footer links — at 400 a 439-chapter series silently lost its
        # first 60 chapters. Only a 6000-char SAMPLE is sent to the LLM; the full
        # list is what the learned regex is matched against.
        links = page.eval_on_selector_all(
            "a[href]", "els => els.slice(0,3000).map(e => [e.getAttribute('href'), (e.textContent||'').trim().slice(0,60)])")
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
    return {"links": links, "imgs": list(dict.fromkeys([*collected, *imgs])), "title": title, "h1": h1}


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

# Any image file, however it is named. Sites do NOT stick to one format: a single
# chapter here served 19 pages as .png and 3 as .jpg, so a rule pinned to one
# extension silently dropped most of the chapter.
_IMAGE_RE = re.compile(r"\.(?:jpe?g|png|webp|gif)(?:[?#]|$)", re.I)


def _dominant_image_host(imgs: list[str]) -> str | None:
    """The CDN host serving this chapter's pages, or None if no host stands out.

    Deliberately does NOT require numbered filenames — plenty of sites use random
    tokens (…/aX9f1756213643.jpg). What identifies the page host is simply that
    most of the images on a chapter page come from it, while logos, avatars and
    ko-fi buttons are one-offs from elsewhere."""
    from collections import Counter
    hosts = [urlparse(u).netloc for u in imgs if _IMAGE_RE.search(u)]
    if not hosts:
        return None
    host, n = Counter(hosts).most_common(1)[0]
    return host if n >= 3 else None   # a real run of pages, not a stray icon


def _rotating_host_rule(imgs: list[str]) -> str | None:
    """Regex for sites that serve pages from MANY rotating CDN hosts under one fixed
    path (https://cdn1.a.site/hi/<token>, https://cdn2.b.site/hi/<token>, ...).
    A single-host rule can't express that, and an LLM tends to overfit the tokens."""
    from collections import defaultdict
    groups: dict[str, list[str]] = defaultdict(list)
    for u in imgs:
        p = urlparse(u)
        seg = p.path.strip("/").split("/")[0] if p.path.count("/") >= 2 else ""
        if seg and p.netloc:
            groups[seg].append(p.netloc)
    if not groups:
        return None
    seg, hosts = max(groups.items(), key=lambda kv: len(kv[1]))
    if len(hosts) >= 8 and len(set(hosts)) >= 4:   # a real run of pages across hosts
        return rf"^https?://[^/]+/{re.escape(seg)}/[^/?#]+$"
    return None


def _norm_url(u: str) -> str:
    """Percent-encode a URL so the same image written two ways collapses to one
    string. Pages are commonly listed BOTH raw and escaped (".../Rent a Girlfriend/"
    and ".../Rent%20a%20Girlfriend/"); without this they de-dupe as two pages and
    every image downloads twice. '%' is in `safe`, so existing escapes survive."""
    from urllib.parse import quote
    return quote(u, safe=":/?#[]@!$&'()*+,;=%~")


# A link that clearly points at a chapter: the word "chapter" followed by a
# number. Used only to notice what a learned regex MISSES — a rule can match
# hundreds of chapters and still quietly drop every fractional one.
_CHAPTERISH_RE = re.compile(r"chapter[-_/]?\d", re.I)


def _missed_chapter_links(rx: str, links: list) -> list[str]:
    """Chapter-looking hrefs that `rx` does NOT match. On this site the learned
    rule ended in (\\d+)/ while fractional chapters are ".../chapter-32-5/", so 18
    chapters were skipped with no error anywhere."""
    try:
        crx = re.compile(rx)
    except re.error:
        return []
    return [h for h, _t in links
            if h and _CHAPTERISH_RE.search(h) and not crx.search(h)]


def _image_rule_problem(rx: str, imgs: list[str], title: str) -> str | None:
    """Why `rx` is unusable as a cached image rule, or None if it looks sound.
    The rule is cached PER DOMAIN and reused for every series on the site, so it
    has to both match this chapter's images and be free of this series' name."""
    try:
        irx = re.compile(rx)
    except re.error as exc:
        return f"it is not a valid Python regex ({exc})"
    if sum(1 for u in imgs if irx.search(u)) < 3:
        return "it matched almost none of the image URLs listed"
    # A regex carrying the series name only ever works for THIS series — the
    # observed failure was "/images/Rent%20a%20Girlfriend/...", which fetches zero
    # pages for every other series on the same site.
    flat = re.sub(r"[^A-Za-z0-9]+", "", rx).lower()
    for word in re.split(r"[^A-Za-z0-9]+", title or ""):
        if len(word) >= 3 and word.lower() in flat:
            return f"it hard-codes the series name ({word!r}) — wildcard that path segment instead"
    return None


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
        # A domain with learned rules cached needs no LLM to be read again.
        return (url.startswith("http") and _is_public_url(url)
                and (bool(_load_rules(urlparse(url).netloc)) or llm_available()))

    def image_headers(self) -> dict[str, str]:
        # Many manga CDNs 403 a hotlinked image without a Referer from the origin
        # site. The AI adapter targets arbitrary sites, so it's the one most likely
        # to need this. Referer is set per-series in fetch_series (cached on the
        # instance); default to empty if fetch_pages runs standalone.
        ref = getattr(self, "_referer", "")
        return {"Referer": ref} if ref else {}

    def _ask_chapter_regex(self, url: str, rendered: dict, problem: str = "") -> dict:
        link_lines = "\n".join(f"{h} | {t}" for h, t in rendered["links"] if h)[:6000]
        extra = f"\nYour previous answer was rejected because {problem}\n" if problem else ""
        return _llm_json(
            "You extract manga chapter lists from a web page. Reply with ONLY a JSON object.",
            f"Series page URL: {url}\nPage title: {rendered['title']}\n\n"
            f"Links on the page (href | text):\n{link_lines}\n{extra}\n"
            "Return JSON with:\n"
            '  "title": the series title (string),\n'
            '  "chapter_regex": a Python regex that MATCHES the chapter hrefs listed '
            "above. It is reused for other series on this site, so replace this series' "
            "name/slug with a wildcard (e.g. [^/]*) — do NOT simply delete it, or the "
            "pattern stops matching. Match fractional chapters too (…/chapter-32-5/ is "
            "chapter 32.5). Exclude nav/login/social/other-series links.",
        )

    def _collect(self, url: str, rendered: dict, rx: str) -> list[tuple[float, str, str]]:
        """Chapter links matching `rx`, de-duped, as (number, absolute_url, text).
        Raises re.error if `rx` is not a valid pattern."""
        crx = re.compile(rx)
        seen, out = set(), []
        for href, text in rendered["links"]:
            if not href or not crx.search(href):
                continue
            full = _abs(url, href)
            if full in seen:
                continue
            seen.add(full)
            numm = re.search(r"(\d+(?:\.\d+)?)", text)
            if numm:
                num = float(numm.group(1))
            else:
                # Fall back to the href, where a fractional chapter is usually
                # written with a separator rather than a point: chapter-32-5 = 32.5.
                hm = re.search(r"chapter[-_/]?(\d+)(?:[-_.](\d+))?", href, re.I)
                if hm:
                    num = float(f"{hm.group(1)}.{hm.group(2)}") if hm.group(2) else float(hm.group(1))
                else:
                    num = 0.0
            out.append((num, full, text.strip()))
        return out

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
        # A rule is only accepted if it actually MATCHES links on this page: an LLM
        # readily returns a plausible-looking regex that matches nothing (e.g. it
        # deletes the series slug from the middle of the path instead of wildcarding
        # it), and caching that would break the whole domain permanently.
        # A rule must match links on THIS page and must not skip any chapter-looking
        # link. Matching *something* is not enough: the first learned rule here
        # matched 449 chapters and silently dropped 18 fractional ones, which shows
        # up much later as a series with holes in it.
        def _judge(rx: str) -> tuple[list, str]:
            """(chapters, reason it is unusable) for a candidate regex."""
            try:
                found = self._collect(url, rendered, rx)
            except re.error as exc:
                return [], f"it is not a valid Python regex ({exc})."
            if not found:
                return [], ("it matched none of the hrefs — the usual cause is DELETING "
                            "the series slug instead of replacing it with a wildcard.")
            missed = _missed_chapter_links(rx, rendered["links"])
            if missed:
                sample = ", ".join(missed[:5])
                return found, (f"it skipped {len(missed)} chapter links, e.g. {sample} — "
                               "fractional chapters use a separator, not a decimal point.")
            return found, ""

        chapters: list[tuple[float, str, str]] = []
        if rules.get("chapter_regex"):
            chapters, why = _judge(rules["chapter_regex"])
            if why:
                # Cached rules are reused for every series on the domain, so a rule
                # with a known flaw is re-learned rather than quietly reapplied.
                print(f"[sources] {domain}: relearning chapter rule — {why}")
                rules.pop("chapter_regex", None)
                chapters = []

        # Try a plain heuristic before the LLM: on essentially every manga site a
        # chapter link is the word "chapter" followed by a number. It costs nothing,
        # is deterministic, and beat the model here — it catches the fractional
        # chapters the LLM's regex kept dropping, with no false positives. The LLM
        # is only needed for sites that name their chapter URLs some other way.
        if not chapters:
            found, why = _judge(_CHAPTERISH_RE.pattern)
            if found and not why:
                chapters, rules["chapter_regex"] = found, _CHAPTERISH_RE.pattern

        if not chapters:
            best, best_rx, why = [], "", ""
            for _attempt in range(3):
                ans = self._ask_chapter_regex(url, rendered, why)
                if ans.get("title"):
                    title = ans["title"]   # the LLM's cleaner title for THIS series
                rx = ans.get("chapter_regex", "")
                found, why = _judge(rx)
                if found and not why:
                    best, best_rx = found, rx
                    break
                if len(found) > len(best):   # keep the least-bad answer so far
                    best, best_rx = found, rx
            if best:
                chapters, rules["chapter_regex"] = best, best_rx
                if why:
                    print(f"[sources] {domain}: best chapter rule still imperfect — {why}")

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
            rot = _rotating_host_rule(sample["imgs"])
            host = None if rot else _dominant_image_host(sample["imgs"])
            if rot:
                rules["image_regex"] = rot
            elif host:
                rules["image_host"] = host
            else:
                img_lines = "\n".join(sample["imgs"])[:6000]
                # Same validate-then-cache loop as the chapter rule: an unchecked
                # regex here is worse than useless, because it's cached per DOMAIN
                # and a series-specific one breaks every other series on the site.
                usable, fallback = "", ""
                problem = ""
                for _attempt in range(2):
                    ans = _llm_json(
                        "You identify the real page images of an online manga chapter. Reply with ONLY JSON.",
                        f"Chapter page URL: {chapter_metas[0].id}\n"
                        f"Series title: {title}\n\n"
                        f"Image URLs on the page:\n{img_lines}\n\n"
                        + (f"Your previous answer was rejected because {problem}.\n\n" if problem else "")
                        + 'Return JSON {"image_regex": <regex>} matching the manga page images. It '
                        "must work for EVERY chapter AND every series on this site — match only the "
                        "stable CDN host + filename pattern; never hard-code chapter-specific "
                        "ids/dates/uuids or the series name (wildcard those path segments, e.g. "
                        "[^/]*). Exclude logos, ads, avatars, thumbnails, icons.",
                    )
                    rx = ans.get("image_regex", "")
                    problem = _image_rule_problem(rx, sample["imgs"], title) or ""
                    if not problem:
                        usable = rx
                        break
                    # A rule that matches but names the series still reads THIS
                    # series correctly — keep it as a last resort over nothing.
                    if rx and "hard-codes" in problem and not fallback:
                        fallback = rx
                if not usable and fallback:
                    print(f"[sources] {domain}: caching a series-specific image rule; "
                          "other series on this site may need a re-import")
                    usable = fallback
                if not usable:
                    raise ValueError("The AI couldn't work out how to find this site's page images.")
                rules["image_regex"] = usable

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
            sel = [u for u in imgs if urlparse(u).netloc == host and _IMAGE_RE.search(u)]
        elif rules.get("image_regex"):
            irx = re.compile(rules["image_regex"])
            sel = [u for u in imgs if irx.search(u)]
        else:
            return []
        sel = list(dict.fromkeys(_norm_url(_abs(chapter.id, u)) for u in sel))
        # `sel` is already in DOM order, which IS the reading order on every site
        # that lays pages out top-to-bottom. Only re-sort when the filenames are
        # genuinely numbered (…/1.jpg, …/2.jpg) and can be trusted to say more
        # than the DOM does — sorting on a number that isn't there gave every URL
        # a key of 0, so a site mixing numbered and token filenames (…/aX9f.jpg)
        # would float the unnumbered pages to the front.
        if sel and all(_NUMBERED_RE.search(u) for u in sel):
            return sorted(sel, key=_page_num)
        return sel

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
