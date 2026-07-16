# MangaShelf Web

A self-hosted web app that turns folders of manga/comics on disk into a
searchable, taggable library you can browse and read from any device on your
home network. A FastAPI backend reads a SQLite database and the on-disk image
library; a React frontend serves the reading experience in the browser.

## What it does

- Serves your library (landing / library / detail / reader) in the browser.
- Reading progress (last chapter + page) is saved back to the DB.
- Browse, read, toggle favorites, set status, and edit metadata (tags / genres /
  notes) from the web.

## Run it

```powershell
# from the project root
.\web\run-web.ps1
# or:  .\.venv\Scripts\python.exe web\server.py
```

Then open **http://localhost:8000** on this PC.

## Frontend build

The React screens are written in `.jsx` and precompiled into a single minified
bundle (`static/app.bundle.js`) by [esbuild](https://esbuild.github.io/) — no
in-browser transpiler ships to the client. After editing any `.jsx` file:

```powershell
cd web
npm install      # first time only — installs esbuild
npm run build    # regenerate static/app.bundle.js
# or, while developing:
npm run watch    # rebuild automatically on every .jsx save
```

`static/app.bundle.js` is generated — don't edit it by hand; edit the `.jsx`
sources and rebuild. Bump the `?v=` token in `static/index.html` on release to
bust the browser cache.

### From your phone (same WiFi)

Set `MANGASHELF_HOST=0.0.0.0` so any device on your home network can reach it:

1. Find this PC's LAN IP: `ipconfig` → IPv4 Address (e.g. `192.168.1.42`).
2. On your phone's browser, go to `http://192.168.1.42:8000`.
3. If it doesn't connect, allow Python through the Windows Firewall on private
   networks (a prompt usually appears the first time the server runs).

## Architecture

- `server.py` — entry point; mounts the API + the static SPA, runs uvicorn.
- `api.py` — FastAPI JSON routes, backed by `database.py` and `scanner.py` from
  the project root. Opens the DB connection with `check_same_thread=False` + a
  lock, since FastAPI serves sync endpoints across a threadpool.
- `build.js` — esbuild build: compiles the `.jsx` sources into `static/app.bundle.js`.
- `static/` — the frontend:
  - `index.html` — loads vendored React + `api.js` + `app.bundle.js`.
  - `styles.css` — the design source.
  - `api.js` — loads real data from `/api/*` (plain JS, loaded before the bundle).
  - `components.jsx`, `screens-*.jsx`, `app.jsx` — the screen sources (covers,
    chapters, pages, progress). Compiled into `app.bundle.js` by `build.js`.
  - `app.bundle.js` — generated; don't edit.
  - `vendor/` — React + ReactDOM, vendored so it works offline.

## API endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/series` | Library list (search/sort/filter params) |
| GET | `/api/series/{id}` | Series detail + real chapters + total pages |
| GET | `/api/series/{id}/chapters` | Chapter list with page counts |
| GET | `/api/series/{id}/cover` | First-page cover image |
| GET | `/api/page?path=...` | Stream one page image (sandboxed to library roots) |
| GET | `/api/facets` | Distinct genres/tags/authors/series for filters |
| POST | `/api/series/{id}/progress` | Save last chapter + page |
| POST | `/api/series/{id}/favorite` | Toggle favorite |
| POST | `/api/series/{id}/status` | Set reading status |
| POST | `/api/series/{id}/metadata` | Edit tags / genres / notes |

The `/api/page` endpoint validates that any requested path lives inside a
configured library folder, so it can't be used to read arbitrary files on disk.

Access is gated by an optional password + per-device token; intended for
trusted home-LAN use.

## Not done yet (good follow-ups)

- Mobile touch polish in the reader (swipe to turn pages, pinch zoom).
- Server-side library search/filter (currently filtered client-side after load;
  fine for a few thousand series, but server params already exist on `/api/series`).
