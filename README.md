# MangaShelf

Self-hosted web app that turns folders of manga/comics on disk into a searchable, taggable library you can browse and read from any device — including offline — with pluggable importers for new series from online sources.

- **Backend:** FastAPI app in `web/api.py` (routers in `web/routes/`), SQLite (`database.py`), folder scanner (`scanner.py`), PDF support, reading lists
- **Frontend:** React (JSX, bundled with esbuild — `cd web && npm run build`), in `web/static/`
- **Library & reader:** faceted search (genres, tags, authors, language, status), ratings/favorites, metadata and cover editing, in-browser reader (vertical + right-to-left) with progress tracking, a Profile screen with reading stats
- **Importing:** MangaDex and AniList adapters, safe declarative JSON source extensions, optional AI fallback; imports run in a persistent server-side queue. "Check all for new chapters" re-syncs every imported series in every library at once.
- **Languages:** each chapter is taken in the first language available: English → Portuguese (PT-PT) → Brazilian Portuguese → the raw original (Japanese/Chinese/Korean); other languages are skipped. When an official English release later appears for a chapter you have in another language, the next check swaps it in (the old copy is kept in `~/.mangashelf/replaced_chapters`).
- **Titles:** English names are shown first when the source has them, and every alternative title (romaji, native script, other languages) is searchable.
- **Machine translation:** raw chapters can be translated to English on your own PC; see below.
- **Offline reading:** save series or individual chapters for offline use (service worker + Cache Storage), "keep the next N chapters", and offline progress that syncs back when you reconnect (newest write wins). In the Android wrapper app, downloads can be stored on an SD card.
- **Hidden tags:** define your own list of tags to hide, with a one-tap Hiding/Showing switch in the top bar; away from home (your phone's public IP differs from the PC's) they are always hidden. Tags can be refreshed from the source for the whole library.
- **Devices:** responsive on phone, tablet and desktop; installable as a PWA; by default only reachable from this PC and your Tailscale devices
- **Tests:** `python -m pytest tests`
- Config lives in `~/.mangashelf/config.json`. Remote access: see `REMOTE_ACCESS.md`. Longer write-up: `PORTFOLIO.md`.

## Machine translation (unofficial)

Chapters that only exist in Japanese, Chinese or Korean can be translated to English locally — nothing leaves your PC:

1. **OCR** (RapidOCR) reads the text on each page and groups the lines into speech bubbles.
2. A **local LLM** translates them in batches, with the previous lines (and the end of the previous chapter) as context, so names and tone stay consistent.
3. Only the text is stored, as `translation.en.json` next to the chapter's images; the images are never modified.
4. The reader draws the English over each bubble: the bubble is painted white and the text fitted inside it; sound effects drawn on the artwork get small outlined text instead, so the art stays visible. **EN (MT) / Original** switches between the two, and the chapter is labelled *Unofficial translation*.

Lines the model returns as romanized Japanese instead of English are retried, then left untranslated rather than shown; OCR debris (stray letters read from linework) is filtered out.

**When it runs:** in the background, only after the PC has been idle for a while (no keyboard/mouse input and a free GPU) — set the idle time, model and auto-translate in Settings → Translation. "Translate" in the reader runs immediately.

**Setup:**
- `pip install -r web/requirements-translate.txt` (into the same environment as the server)
- [Ollama](https://ollama.com) with a model, e.g. `ollama pull gemma3:12b` (the default)
- Optional, faster and better: a mixture-of-experts GGUF model served by llama.cpp's `llama-server`, with most expert weights kept in system RAM. The batch worker starts it only while the PC is idle and stops it when you come back:
  ```powershell
  .venv\Scripts\python.exe web\mt_batch.py --idle 10 --gguf D:\models\your-moe-model.gguf --n-cpu-moe 34
  ```
- Machine-local settings (fallback model, extra prompt notes such as a glossary) go in `~/.mangashelf/mt_local.json`: `{"fallback_model": "...", "prompt_extra": "..."}`.

Progress of the batch worker is logged to `~/.mangashelf/mt_batch.log`.

---

## Original design handoff note (historical)

This is a **handoff bundle** from Claude Design (claude.ai/design).

A user mocked up designs in HTML/CSS/JS using an AI design tool, then exported this bundle so a coding agent can implement the designs for real.

## What you should do — IMPORTANT

**Find the primary design file under `mangashelf/project/` and read it top to bottom.** Then **follow its imports**: open every file it pulls in (shared components, CSS, scripts) so you understand how the pieces fit together before you start implementing.

**If anything is ambiguous, ask the user to confirm before you start implementing.** It's much cheaper to clarify scope up front than to build the wrong thing.

## About the design files

The design medium is **HTML/CSS/JS** — these are prototypes, not production code. Your job is to **recreate them pixel-perfectly** in whatever technology makes sense for the target codebase (React, Vue, native, whatever fits). Match the visual output; don't copy the prototype's internal structure unless it happens to fit.

**Don't render these files in a browser or take screenshots unless the user asks you to.** Everything you need — dimensions, colors, layout rules — is spelled out in the source. Read the HTML and CSS directly; a screenshot won't tell you anything they don't.

## Bundle contents

- `mangashelf/README.md` — this file
- `mangashelf/project/` — the `mangashelf` project files (HTML prototypes, assets, components)
