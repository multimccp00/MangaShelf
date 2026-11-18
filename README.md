# MangaShelf

Self-hosted web app that turns folders of manga/comics on disk into a searchable, taggable library you can browse and read from any device, with pluggable importers for new series from online sources.

- **Backend:** FastAPI app in `web/api.py`, SQLite (`database.py`), folder scanner (`scanner.py`), PDF support, reading lists
- **Frontend:** React, in `web/`
- **Features:** faceted search (genres, tags, authors, language, status), ratings/favorites, metadata and cover editing, in-browser reader (vertical + right-to-left) with progress tracking
- Config lives in `~/.mangashelf/config.json`. Remote access: see `REMOTE_ACCESS.md`. Longer write-up: `PORTFOLIO.md`.

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
