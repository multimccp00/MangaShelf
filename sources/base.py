"""Source-adapter interface for importing series from an external URL.

A "source" knows how to turn a series URL into metadata + a chapter list, and how
to resolve a chapter into its page image URLs. The app ships adapters only for
sources that expose an official public API (see mangadex.py). Additional adapters
can be dropped into sources/local/ (see registry.py) — that folder is git-ignored,
so user-supplied adapters never enter the repository.

The download/ingest side (writing files, metadata sidecar, DB rescan) is shared
across all adapters in downloader.py — an adapter only implements the three
site-specific methods below.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ChapterMeta:
    """One chapter of a series, as seen by a source adapter."""
    id: str                 # source-specific handle used to fetch pages
    number: str = ""        # "1", "12.5", or "" for one-shots
    title: str = ""         # optional chapter title
    language: str = ""      # translated language code, when the source has it
    url: str = ""           # optional canonical URL (for reference/logging)


@dataclass
class SeriesMeta:
    """Everything an adapter can tell us about a series before downloading it."""
    source: str                                   # adapter name, e.g. "mangadex"
    external_id: str                              # stable id within that source
    title: str
    author: str = ""
    genres: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    description: str = ""
    cover_url: str = ""
    chapters: list[ChapterMeta] = field(default_factory=list)


class MangaSource(ABC):
    """A pluggable import source. Implement the three abstract methods."""

    #: short, stable identifier used in logs and stored in external_id ("<name>:<id>")
    name: str = "base"
    #: human-facing label shown in the UI
    label: str = "Source"
    #: example series URL shown as a hint in the import dialog
    example: str = ""

    @abstractmethod
    def matches(self, url: str) -> bool:
        """True if this adapter can handle the given series URL."""

    @abstractmethod
    def fetch_series(self, url: str) -> SeriesMeta:
        """Resolve a series URL into metadata + the full ordered chapter list.
        Must NOT download page images (that's fetch_pages, called per chapter)."""

    @abstractmethod
    def fetch_pages(self, chapter: ChapterMeta) -> list[str]:
        """Return the ordered list of page-image URLs for one chapter."""

    def image_headers(self) -> dict[str, str]:
        """Extra HTTP headers required to fetch this source's images (e.g. a
        Referer). Empty by default."""
        return {}

    def url_for(self, external_id: str) -> str:
        """Rebuild the canonical series URL from the stored id, so the app can
        link back to the origin and re-sync. Empty if not reconstructable."""
        return ""
