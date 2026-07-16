# Import sources — roadmap (v2+)

v1 (done): pluggable source adapters, a MangaDex reference adapter (official API),
a shared polite/resumable downloader + `metadata.json` sidecar, `/api/scrape`
preview/start/status endpoints, and the "Import from a link" modal. Imported
series record their origin as `external_id = "<source>:<id>"`.

Everything below is deferred. Kept here so it isn't forgotten.

## 1. Additional adapters (user-provided)
Adapters for sources without an official API live in `sources/local/`
(git-ignored) and are auto-discovered — see [`README.md`](README.md). Two shapes
to support there:
- **Plain HTML sources** — parse the page for its image URLs; some need a
  `Referer` header on image requests (override `image_headers()`).
- **Protected sources** — behind a bot challenge (Cloudflare, etc.); these need
  the browser-driven fetch from §4 rather than plain HTTP.

## 2. Re-sync / "check for new chapters"
- Use the stored `external_id` to re-run the adapter for an existing series and
  download only chapters not already on disk (the downloader is already resumable
  per-file; this adds per-chapter skip by number).
- UI: a "Check for updates" action on the detail screen; a bulk "sync all" later.
- Needs a DB lookup by `external_id` (already a column) to map a series back to
  its folder.

## 3. Adapter niceties
- **Cover**: save the scraped `cover_url` as the series' custom cover instead of
  relying on auto-resolution from page 1 (cleaner cover for long-strip series).
- **MangaDex language**: currently hard-coded to English (`translatedLanguage=en`).
  Expose a language picker in the preview step.
- **MangaDex content rating**: currently uses the API defaults. Any change to that
  is a per-user setting — keep it out of the committed default.
- **Parallel page downloads**: a small bounded pool (e.g. 3-4 concurrent) would
  cut import time a lot; keep the politeness delay per host.

## 4. Smart / AI-assisted adapter (the big one)
Goal: paste a link from a site with **no** adapter and still import it.
- A local LLM (Ollama/llama.cpp) + Playwright (already a dev dependency) inspects
  the page, identifies the chapter-list selector and the page-image pattern, and
  emits/caches a deterministic adapter for that domain.
- AI runs at **design/repair time only** (once per new or broken domain), never in
  the download loop. Cache the generated adapter under `sources/local/`.
- Browser-driven fetching (via Playwright) also handles bot-challenge / JS-heavy
  sites, covering §1's "protected sources" case.

## 5. UX / robustness
- Duplicate detection: if `external_id` already exists, offer **Sync** instead of a
  fresh import.
- Cancel button on an in-progress import (cooperative flag checked between chapters).
- Per-source help text + URL validation in the modal.
- Surface skipped/failed pages in the final status (don't silently drop).
