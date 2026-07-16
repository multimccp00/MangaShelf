# Import sources

A small plugin system for importing a series from an external URL into your
library. Paste a series link in the app ("Add from link"); the matching adapter
resolves its metadata + chapter list, the shared downloader fetches the pages into
your library folder, and a rescan ingests it like any other series.

## Design

```
sources/
  base.py        MangaSource interface + SeriesMeta / ChapterMeta
  registry.py    finds the adapter for a URL; loads built-ins + local/ plugins
  downloader.py  shared, polite, resumable page downloader + metadata sidecar
  mangadex.py    reference adapter (official MangaDex API)
  local/         user-provided adapters (git-ignored)
```

An adapter implements just three site-specific methods:

```python
class MangaSource(ABC):
    def matches(self, url) -> bool
    def fetch_series(self, url) -> SeriesMeta      # metadata + chapter list
    def fetch_pages(self, chapter) -> list[str]    # page image URLs
```

Everything else — downloading, folder layout, the `metadata.json` sidecar, the DB
rescan — is shared, so an adapter never touches the filesystem or the database.

## What ships with the app

Only sources that expose an **official public API** are bundled — currently
**MangaDex**. This keeps the shipped code stable (no HTML scraping that breaks on
redesigns) and sanctioned.

## Adding your own source

Drop a module into [`local/`](local/) that subclasses `MangaSource`; it's
auto-discovered at startup. That folder is git-ignored (except its `__init__.py`),
so your adapters stay on your machine. See `local/__init__.py` for a template.

## Re-sync (planned)

Each imported series records its origin as `external_id = "<source>:<id>"`, so a
future "check for new chapters" action can re-run the adapter and download only
what's missing. See [`ROADMAP.md`](ROADMAP.md).
