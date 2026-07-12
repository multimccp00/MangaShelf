# MangaShelf — Site Extensions: Design

**Status:** IMPLEMENTED 2026-07-12 (all 5 tiers). Engine `declarative.py`, loader/mgmt in `registry.py`, API in `api.py` (`/api/sources/extensions*`), UI `SourceExtensionsModal` in `app.jsx`, AI export `manifest_from_ai_rules` in `ai.py`. Verified unit-level (validation, routing, SSRF, JSON path, AI round-trip) + UI smoke test. **Pending: one real-site end-to-end import** (needs the user's server restarted so the new endpoints/engine load).
**Date:** 2026-07-12
**Goal:** Let users add support for a new manga site by installing a small **extension file**, and update a site that changed by swapping just that one file — safely, in a multi-user app where extensions may be authored by people you don't trust.

---

## 1. The core decision: extensions are DATA, not CODE

The app is intended for **multiple, untrusted users**. That single fact rules out the current mechanism (`sources/local/*.py` auto-imported at startup) as the *user-facing* path:

> A dropped `.py` extension runs with full server privileges — it can read the library, the auth token (`~/.mangashelf/web_token.txt`), the password hash, make arbitrary network calls, and delete files. If user A can hand user B an "extension," that's remote code execution by design.

**Therefore: a user extension is a declarative manifest** (JSON) that *describes* how to read a site — selectors, URL patterns, regexes — and is interpreted by **one trusted engine** the app ships. The manifest cannot `import`, cannot run code, cannot touch the filesystem. A malicious manifest can only produce wrong data or fail — never attack the host.

This mirrors how the mature apps in this space handle it (Tachiyomi/Mihon extensions are signed APKs sandboxed by Android; we get equivalent safety cheaply by never executing extension-supplied code at all).

### The three-tier source model

| Tier | Mechanism | Trust | Committed? | Status |
|------|-----------|-------|-----------|--------|
| **Built-in API** | Python `MangaSource` (e.g. MangaDex) | App author | Yes | ✅ exists |
| **Declarative extension** | JSON manifest + `DeclarativeSource` engine | **Untrusted / user-shared** | Engine yes, manifests no | 🆕 this design |
| **Private dev adapter** | `sources/local/*.py` (git-ignored) | Machine owner only | No | ✅ exists — keep for your own use |
| **AI fallback** | LLM infers rules for an unknown site | App author | Yes | ✅ exists |

The existing `sources/local/*.py` path **stays** — it's how *you* (the trusted operator) run private adapters like the piracy/adult ones. What's new is the safe, shareable, user-installable declarative tier.

---

## 2. What already exists (we build ON this, not around it)

- **`base.py`** — the `MangaSource` contract (`matches` / `fetch_series` / `fetch_pages` → `SeriesMeta` / `ChapterMeta`). The manifest engine is just one more `MangaSource`.
- **`registry.py._load_local()`** — already auto-discovers files in a folder. We add a parallel `_load_extensions()` for manifests.
- **`ai.py`** — already renders JS sites (Playwright), already reverse-engineers a site into cached rules (`_ai_cache/<domain>.json` = `{chapter_regex, image_host}`), already has the **SSRF guard** (`_is_public_url`) and Referer handling. The AI cache is *almost a manifest already* → the AI can **generate** extensions (see §7).
- **`downloader.py`** — shared, polite, resumable, size-capped page download. Unchanged; the engine just feeds it URLs.

The manifest engine only has to output `SeriesMeta`/`ChapterMeta`; everything downstream already works.

---

## 3. The manifest format (v1)

A extension is one JSON file. Two site *types* cover the vast majority of manga sites:

- `"html"` — parse the page's HTML (selectors / regex). Most aggregators.
- `"json"` — the site has a JSON endpoint (like MangaDex). Cleaner when available.

### 3.1 HTML manifest example

```json
{
  "manifest_version": 1,
  "id": "examplescans",
  "name": "Example Scans",
  "version": "1.0.0",
  "author": "community",
  "type": "html",
  "match": { "host": "examplescans.org", "path_contains": "/manga/" },
  "example_url": "https://examplescans.org/manga/<slug>/",
  "needs_browser": false,
  "series": {
    "title":  { "css": "h1.series-title" },
    "author": { "css": ".author-name" },
    "cover":  { "css": ".cover img", "attr": "src" },
    "genres": { "css": ".genre-tag", "all": true },
    "chapter_links": {
      "css": "a.chapter-item",
      "attr": "href",
      "number_from_text": "(\\d+(?:\\.\\d+)?)"
    }
  },
  "pages": {
    "image_css": "img.page-image",
    "attr": "data-src",
    "url_regex": "/(\\d+)\\.(?:jpg|png|webp)"
  },
  "headers": { "referer": "origin" }
}
```

### 3.2 JSON manifest example (API-style site)

```json
{
  "manifest_version": 1,
  "id": "somemangaapi",
  "name": "Some Manga API",
  "version": "1.0.0",
  "type": "json",
  "match": { "host": "somemanga.example", "path_regex": "/title/([0-9a-f-]+)" },
  "series": {
    "endpoint": "https://api.somemanga.example/manga/{id}",
    "title":   "$.data.attributes.title.en",
    "author":  "$.data.author.name",
    "cover":   "https://cdn.somemanga.example/{id}/{$.data.cover}",
    "chapters_endpoint": "https://api.somemanga.example/manga/{id}/feed",
    "chapter_id":     "$.data[*].id",
    "chapter_number": "$.data[*].attributes.chapter"
  },
  "pages": {
    "endpoint": "https://api.somemanga.example/at-home/{chapter_id}",
    "image_template": "{$.baseUrl}/data/{$.hash}/{$.files[*]}"
  }
}
```

### 3.3 Field reference (v1 scope)

- **`match`** — how a URL is routed to this extension. `host` (substring) + optional `path_contains` / `path_regex`. Drives `matches()`.
- **HTML selectors** — `{css, attr?, all?, regex?, number_from_text?}`. `attr` defaults to text content; `all:true` returns a list (genres); `regex` extracts from the matched string.
- **JSON paths** — a small **safe subset of JSONPath** (`$.a.b`, `$.a[*].b`, `{...}` interpolation). We implement the subset ourselves — no eval, no third-party JSONPath that could be abused.
- **`needs_browser`** — if true, render with Playwright (reuse `ai.py`'s `_render`, including scroll-for-lazy-images). Off by default (plain HTTP is faster/safer).
- **`headers.referer: "origin"`** — send `Referer: https://<host>/` on image fetches (the hotlink-protection fix already in `image_headers()`).

Anything a v1 manifest can't express falls back to the **AI adapter** (which stays the catch-all).

---

## 4. The engine: `DeclarativeSource` (trusted, committed)

One new file, `sources/declarative.py`, committed and reviewed. Shape:

```python
class DeclarativeSource(MangaSource):
    def __init__(self, manifest: dict): self.m = manifest; ...
    def matches(self, url):     # uses self.m["match"], + _is_public_url(url) SSRF guard
    def fetch_series(self, url): # HTML: fetch/render → apply selectors → SeriesMeta
    def fetch_pages(self, ch):   # apply page rules → list[str]
    def image_headers(self):     # referer from manifest
```

Hard safety rules the engine enforces:
- **Every fetch goes through `_is_public_url()`** (the SSRF guard from `ai.py`) — an extension can never point the server at `localhost`/`169.254.169.254`/LAN.
- **Size caps** on every read (reuse the `_MAX_*_BYTES` pattern already added to the downloader/cover paths).
- **No code execution** — selectors and the JSON-path subset are interpreted, never `eval`'d. Regexes are compiled with a timeout guard (catch catastrophic backtracking).
- **Total-timeout** per operation so a hostile manifest can't hang a worker forever.

HTML parsing: use `selectolax` or the stdlib `html.parser` + the existing regex approach (no heavy new dep if avoidable — the AI adapter already does regex-on-rendered-HTML successfully).

---

## 5. Loading & storage

- **Location:** `~/.mangashelf/extensions/*.json` (user data dir, NOT the repo — same place covers/settings live). Never committed; per-install.
- **`registry._load_extensions()`** — parallel to `_load_local()`: read every `*.json`, validate against the schema, wrap valid ones in `DeclarativeSource`, register. A malformed/invalid manifest is **skipped with a logged reason**, never crashes discovery (same resilience as `_load_local`).
- **Ordering:** built-in API adapters → declarative extensions → private local `.py` → AI fallback last. Dedicated always beats AI.
- **Hot-reload:** add `registry.reload()` that clears the `_sources` cache. Called by the install/enable/disable endpoints so **no server restart is needed** — directly serves your "swap one file when a site changes" goal.

---

## 6. UI & API

New endpoints (all token-gated, mutations require the header):
- `GET  /api/extensions` — list installed: id, name, version, author, host, enabled, valid?
- `POST /api/extensions` — install from pasted JSON or uploaded file. **Validate before saving.** Show a clear "review before installing — extensions can fetch from the site they name" notice.
- `POST /api/extensions/{id}/toggle` — enable/disable without deleting.
- `DELETE /api/extensions/{id}` — remove.
- `POST /api/extensions/validate` — dry-run a manifest against its `example_url` and report what it extracted (title/chapter count) so an author can test.

UI: a new "Extensions" section in the existing Settings modal (it already has the pattern) — list, install (paste/file), enable/disable/remove, and a "Test" button that runs `validate`. Reuses the modal/toast/confirm components already built.

---

## 7. The payoff: AI generates extensions

`ai.py` already infers `{chapter_regex, image_host}` for an unknown domain and caches it. Add one step: **export that inference as a manifest**. Flow becomes:

1. User pastes an unknown site → AI figures it out (existing).
2. App offers: *"Save this as an extension?"* → writes a `type: "html"` manifest from the AI's rules.
3. That manifest is now a **shareable file** — the user can send it to others, who install it **without needing an LLM** and get deterministic, fast imports.

This turns the AI from a per-user convenience into a **community content generator**, and means most extensions get authored by the AI, not hand-written.

---

## 8. Build order (each independently shippable)

1. **Manifest schema + `DeclarativeSource` engine** (HTML type first) — the core. Verify against one real site end-to-end.
2. **`_load_extensions()` + `registry.reload()`** — discovery + hot-reload from `~/.mangashelf/extensions/`.
3. **API + Settings UI** — install/list/toggle/remove/test.
4. **JSON manifest type** — for API-style sites.
5. **AI → manifest export** — "save discovered site as extension."

Tiers 1–3 deliver the feature you asked for. 4–5 are force-multipliers.

---

## 9. Explicitly out of scope for v1 (and why)

- **A public extension "store"/repo** — that's a distribution/moderation problem (who vets uploads? takedowns for piracy?). v1 is install-from-file; a registry can come later.
- **Sandboxed Python extensions** — rejected: the declarative format gives the safety without the complexity of a real sandbox (subprocess isolation, seccomp, resource limits). If a site truly can't be expressed declaratively, the AI adapter covers it.
- **Signing/verification** — deferred. v1 relies on "install only what you trust" + the fact that manifests can't execute code, so the blast radius of a bad one is limited to bad data.

---

## 10. Decisions (locked 2026-07-12)

1. **Piracy/legal:** ✅ Engine ships **neutral**; manifests are **user-supplied, never bundled** in the repo. The committed code contains no site-specific scraping manifests — users add their own. This keeps the app off the liability hook.
2. **Parsing:** ✅ **Regex + stdlib only — NO new dependency.** Confirmed only `html.parser`/`re`/`urllib`/`json` + Playwright are available. The manifest uses **regex patterns**, exactly like the AI adapter's `_ai_cache` (`chapter_regex` + `image_host`) already does successfully on real sites. Bonus: an AI-generated manifest maps 1:1 to what the AI already produces. (§3 examples show CSS for readability, but the v1 implementation uses the regex fields — `url_regex`, `number_from_text`, `image url_regex` — and `image_host`.)
3. **Naming:** ✅ **"Sources"** — matches the existing `sources/` folder, `/api/sources`, and the "Supported sources" UI hint. No rename needed.

### Regex-manifest shape (v1 actual, HTML type)
```json
{
  "manifest_version": 1,
  "id": "examplescans",
  "name": "Example Scans",
  "version": "1.0.0",
  "author": "community",
  "type": "html",
  "match": { "host": "examplescans.org", "path_contains": "/manga/" },
  "example_url": "https://examplescans.org/manga/<slug>/",
  "needs_browser": false,
  "series": {
    "title_regex":        "<h1[^>]*>(.*?)</h1>",
    "chapter_link_regex": "href=\"(/manga/[^\"]+?/chapter-\\d+[^\"]*)\"",
    "chapter_number_regex": "chapter-(\\d+(?:\\.\\d+)?)"
  },
  "pages": {
    "image_host": "cdn.examplescans.org",
    "image_url_regex": "/(\\d+)\\.(?:jpg|png|webp)"
  },
  "headers": { "referer": "origin" }
}
```
This is deliberately near-identical to `_ai_cache/<domain>.json` so the AI export in §7 is a thin wrapper.
```
